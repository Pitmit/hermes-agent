"""Kanban task-bound independent watchdog (governance stage 5).

Spec: docs/kanban-governance-spec.md §7 (Stufe 5). These tests prove the
card's contract:

* exactly-once per stopped-state fingerprint — a stop fires ONE firing row
  and ONE ``watchdog_fired`` event; re-checking the same stop changes
  nothing; a NEW fingerprint (changed subject) may fire again;
* no self-review — the reviewer may never be the task's own
  assignee/implementer (create-time AND decide-time fences), and the
  worker decide path is reserved for the watchdog's own reviewer profile;
* no fix by the watchdog — the decision vocabulary is exactly
  accept/request_changes/reopen/reassign; every other verb is refused and
  no decision mutates the task's content (body/result/artifacts);
* three identical rounds escalate stickily — unchanged fingerprints count
  rounds; at the limit the task routes to ``triage`` with exactly ONE
  ``watchdog_escalated`` event and the watchdog freezes (sticky human
  escalation, a later check is silent);
* old cards stay unchanged — tasks WITHOUT a watchdog are never touched by
  the check phase or a flag-on dispatcher tick (status diff empty, zero
  watchdog events);
* human gates stay human — ``reopen`` refuses needs_input/capability
  blocks;
* additive migration — a pre-watchdog board DB re-creates the tables and
  serves;
* dispatcher integration is flag-gated (off = ``watchdog_report`` None,
  byte-identical tick; on = the phase runs) and the config keys are live
  through the real loader.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_watchdog as kw


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The test process may itself be a dispatched kanban worker — strip its
    # board/task env so board resolution AND the fences are deterministic.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def watchdog_on(kanban_home):
    """``kanban.watchdog.enabled: true`` through the real config loader."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  watchdog:\n    enabled: true\n"
    )
    return kanban_home


@pytest.fixture
def watchdog_tick_on(kanban_home):
    """``kanban.watchdog.tick_enabled: true`` through the real config loader."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  watchdog:\n    tick_enabled: true\n"
    )
    return kanban_home


# ---------------------------------------------------------------- helpers


def _board_slug() -> str:
    return kb.get_current_board() or kb.DEFAULT_BOARD


def _connect():
    return kbc.connect()


def _task(conn, *, assignee="impl", title="watched", **kw) -> str:
    return kb.create_task(conn, title=title, assignee=assignee, **kw)


def _watch(conn, task_id, *, reviewer="sdlc-review", instructions="check the handoff",
           created_by="peter", **extra):
    return kw.create_watchdog(
        conn, board=_board_slug(), task_id=task_id, reviewer=reviewer,
        instructions=instructions, created_by=created_by, **extra,
    )


def _events(conn, task_id, kind):
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()[0])


def _status(conn, task_id):
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return row["status"]


def _firings(conn, watchdog_id):
    return conn.execute(
        "SELECT * FROM task_watchdog_firings WHERE watchdog_id = ? ORDER BY id",
        (watchdog_id,),
    ).fetchall()


def _snapshot(conn):
    """All task statuses + watchdog-irrelevant fields, for statusdiffs."""
    rows = conn.execute(
        "SELECT id, status, assignee, body, result FROM tasks ORDER BY id"
    ).fetchall()
    return {r["id"]: (r["status"], r["assignee"], r["body"], r["result"]) for r in rows}


def _review_stop(conn, task_id, summary="same summary"):
    assert kb.request_review(conn, task_id, summary=summary)


def _check(conn, task_id=None):
    return kw.check_watchdogs(conn, _board_slug(), task_id=task_id)


# ---------------------------------------------------------------- migration


def test_legacy_db_creates_watchdog_tables_and_serves(kanban_home):
    conn = _connect()
    tid = _task(conn)
    conn.close()

    # Simulate a pre-governance board: stage-5 tables absent.
    path = kb.kanban_db_path()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE IF EXISTS task_watchdogs")
    raw.execute("DROP TABLE IF EXISTS task_watchdog_firings")
    raw.commit()
    raw.close()
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))

    # The migrated board re-creates the tables additively and serves reads
    # AND a watchdog attach.
    conn = _connect()
    assert any(t.id == tid for t in kb.list_tasks(conn))
    wd = _watch(conn, tid)
    assert wd["status"] == "active"
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    conn.close()
    conn = _connect()
    assert kw.watchdog_for_task(conn, _board_slug(), tid)["id"] == wd["id"]
    assert _events(conn, tid, "watchdog_created") == 1
    conn.close()


# ---------------------------------------------------------------- create fences


def test_create_refuses_self_review_assignee_and_implementer(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="alice")
    with pytest.raises(PermissionError, match="no self-review"):
        _watch(conn, tid, reviewer="alice")
    # implementer fence: a claimed run's profile is the implementer
    tid2 = _task(conn, assignee="bob")
    assert kb.claim_task(conn, tid2) is not None
    with pytest.raises(PermissionError, match="no self-review"):
        _watch(conn, tid2, reviewer="bob")
    # an independent reviewer passes
    wd = _watch(conn, tid2, reviewer="carol")
    assert wd["reviewer"] == "carol"
    conn.close()


def test_create_refuses_unknown_task_and_duplicate_active(kanban_home):
    conn = _connect()
    with pytest.raises(ValueError, match="does not exist"):
        _watch(conn, "t_missing")
    tid = _task(conn)
    _watch(conn, tid)
    with pytest.raises(ValueError, match="already has an active watchdog"):
        _watch(conn, tid, reviewer="someone-else")
    # removing (retiring) frees the slot; the row stays auditable
    first = kw.list_watchdogs(conn, _board_slug())[0]
    kw.remove_watchdog(conn, first["id"], removed_by="peter")
    assert kw.list_watchdogs(conn, _board_slug(), status="active") == []
    assert kw.list_watchdogs(conn, _board_slug(), status="retired")
    _watch(conn, tid, reviewer="another-reviewer")
    assert _events(conn, tid, "watchdog_removed") == 1
    conn.close()


# ---------------------------------------------------------------- exactly-once


def test_fires_exactly_once_per_fingerprint(kanban_home):
    conn = _connect()
    tid = _task(conn)
    wd = _watch(conn, tid)
    _review_stop(conn, tid)

    report = _check(conn)
    assert [e["task_id"] for e in report["fired"]] == [tid]
    # re-checks of the SAME stop: no second firing, no second event
    for _ in range(3):
        again = _check(conn)
        assert again["fired"] == [] and again["rounds"] == []
    assert len(_firings(conn, wd["id"])) == 1
    assert _events(conn, tid, "watchdog_fired") == 1
    conn.close()


def test_new_fingerprint_fires_again(kanban_home):
    conn = _connect()
    tid = _task(conn)
    wd = _watch(conn, tid)
    _review_stop(conn, tid, summary="first")
    _check(conn)
    assert len(_firings(conn, wd["id"])) == 1

    # leave the stopped state, then stop again with a CHANGED subject
    assert kb.reopen_review_task(conn, tid)
    _review_stop(conn, tid, summary="changed subject")
    report = _check(conn)
    assert [e["task_id"] for e in report["fired"]] == [tid]
    rows = _firings(conn, wd["id"])
    assert len(rows) == 2
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
    assert rows[1]["rounds"] == 1
    assert _events(conn, tid, "watchdog_fired") == 2
    conn.close()


def test_blocked_stop_fires_once_and_holds(kanban_home):
    conn = _connect()
    tid = _task(conn)
    wd = _watch(conn, tid)
    assert kb.block_task(conn, tid, reason="quota exhausted", kind="transient")
    _check(conn)
    _check(conn)
    rows = _firings(conn, wd["id"])
    assert len(rows) == 1
    assert rows[0]["stop_kind"] == "blocked"
    assert _events(conn, tid, "watchdog_fired") == 1
    conn.close()


# ---------------------------------------------------------------- no fix / no self-decide


def test_decision_vocabulary_is_closed_no_repair_verb(kanban_home):
    conn = _connect()
    tid = _task(conn)
    _watch(conn, tid)
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    for verb in ("fix", "repair", "complete", "unblock", "approve", "edit", ""):
        with pytest.raises(ValueError, match="never repairs"):
            kw.decide_firing(conn, wd_id, verb=verb, decider="sdlc-review")
    assert kw.WATCHDOG_VERBS == {"accept", "request_changes", "reopen", "reassign"}


def test_decide_never_mutates_task_content(kanban_home):
    conn = _connect()
    tid = _task(conn)
    _watch(conn, tid)
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    before = conn.execute(
        "SELECT title, body, result, completion_contract FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()

    kw.decide_firing(conn, wd_id, verb="accept", decider="sdlc-review", note="ok")
    after = conn.execute(
        "SELECT title, body, result, completion_contract FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()
    assert tuple(before) == tuple(after)
    assert _events(conn, tid, "watchdog_decided") == 1


def test_decide_refuses_self_review(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="alice")
    _watch(conn, tid, reviewer="sdlc-review")
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    # the task's own assignee can never decide its watchdog — even via CLI
    with pytest.raises(PermissionError, match="no self-review"):
        kw.decide_firing(conn, wd_id, verb="accept", decider="alice", via="cli")
    conn.close()


def test_worker_decide_reserved_for_watchdog_reviewer(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="alice")
    _watch(conn, tid, reviewer="sdlc-review")
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    with pytest.raises(PermissionError, match="reserved for the watchdog's reviewer"):
        kw.decide_firing(conn, wd_id, verb="accept", decider="some-other-worker",
                         via="tool")
    # the watchdog's own reviewer decides fine through the tool path
    result = kw.decide_firing(conn, wd_id, verb="accept", decider="sdlc-review",
                              via="tool")
    assert result["firing"]["outcome"] == "accepted"
    assert result["firing"]["decided_via"] == "tool"
    assert result["firing"]["outcome_fingerprint"]
    conn.close()


def test_decision_applies_exactly_once(kanban_home):
    conn = _connect()
    tid = _task(conn)
    _watch(conn, tid)
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    kw.decide_firing(conn, wd_id, verb="accept", decider="sdlc-review")
    with pytest.raises(kw.WatchdogStateError, match="exactly once"):
        kw.decide_firing(conn, wd_id, verb="accept", decider="sdlc-review")
    assert _events(conn, tid, "watchdog_decided") == 1
    # a watchdog with no firing at all is refused, not invented
    tid2 = _task(conn, title="unwatched stop")
    wd2 = _watch(conn, tid2, reviewer="sdlc-review")
    with pytest.raises(kw.WatchdogStateError, match="no firing"):
        kw.decide_firing(conn, wd2["id"], verb="accept", decider="sdlc-review")
    conn.close()


# ---------------------------------------------------------------- verb effects


def test_request_changes_routes_back_to_implementer(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="impl")
    _watch(conn, tid, reviewer="sdlc-review")
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    result = kw.decide_firing(conn, wd_id, verb="request_changes",
                              decider="sdlc-review", note="needs work")
    assert result["firing"]["outcome"] == "changes_requested"
    assert _status(conn, tid) == "ready"
    assert conn.execute("SELECT assignee FROM tasks WHERE id = ?",
                        (tid,)).fetchone()["assignee"] == "impl"
    assert _events(conn, tid, "changes_requested") == 1
    # request_changes is review-only: a blocked stop refuses it
    tid2 = _task(conn, title="blocked card")
    _watch(conn, tid2, reviewer="sdlc-review")
    kb.block_task(conn, tid2, reason="r", kind="transient")
    _check(conn)
    wd2_id = kw.watchdog_for_task(conn, _board_slug(), tid2)["id"]
    with pytest.raises(kw.WatchdogStateError, match="review stops only"):
        kw.decide_firing(conn, wd2_id, verb="request_changes", decider="sdlc-review")
    conn.close()


def test_reopen_semantics_and_human_gate_refusal(kanban_home):
    conn = _connect()
    # review stop: review_reopened semantics
    tid = _task(conn, assignee="impl")
    _watch(conn, tid, reviewer="sdlc-review")
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    kw.decide_firing(conn, wd_id, verb="reopen", decider="sdlc-review")
    assert _status(conn, tid) == "ready"
    assert _events(conn, tid, "review_reopened") == 1

    # transient block: the shared unblock semantics
    tid2 = _task(conn, title="flaky")
    _watch(conn, tid2, reviewer="sdlc-review")
    kb.block_task(conn, tid2, reason="api hiccup", kind="transient")
    _check(conn)
    wd2_id = kw.watchdog_for_task(conn, _board_slug(), tid2)["id"]
    kw.decide_firing(conn, wd2_id, verb="reopen", decider="sdlc-review")
    assert _status(conn, tid2) == "ready"
    assert _events(conn, tid2, "unblocked") == 1

    # human gate: needs_input is NEVER reopened by the watchdog
    tid3 = _task(conn, title="needs a human")
    _watch(conn, tid3, reviewer="sdlc-review")
    kb.block_task(conn, tid3, reason="question for the operator", kind="needs_input")
    _check(conn)
    wd3_id = kw.watchdog_for_task(conn, _board_slug(), tid3)["id"]
    with pytest.raises(PermissionError, match="human gate"):
        kw.decide_firing(conn, wd3_id, verb="reopen", decider="sdlc-review")
    assert _status(conn, tid3) == "blocked"
    conn.close()


def test_reassign_hands_off_assignee(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="impl")
    _watch(conn, tid, reviewer="sdlc-review")
    _review_stop(conn, tid)
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    # reassign needs the new assignee
    with pytest.raises(ValueError, match="requires the new assignee"):
        kw.decide_firing(conn, wd_id, verb="reassign", decider="sdlc-review")
    result = kw.decide_firing(conn, wd_id, verb="reassign", decider="sdlc-review",
                              assignee="senior-impl")
    assert result["firing"]["outcome"] == "reassigned"
    assert conn.execute("SELECT assignee FROM tasks WHERE id = ?",
                        (tid,)).fetchone()["assignee"] == "senior-impl"
    assert _events(conn, tid, "assigned") == 1
    conn.close()


# ---------------------------------------------------------------- round limit


def test_three_identical_rounds_escalate_stickily(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="impl")
    wd = _watch(conn, tid, reviewer="sdlc-review")

    # round 1: the first stop fires
    _review_stop(conn, tid, summary="unchanged")
    report = _check(conn)
    assert len(report["fired"]) == 1 and report["escalated"] == []

    # round 2: same fingerprint after leaving + re-entering the stop
    kb.reopen_review_task(conn, tid)
    _review_stop(conn, tid, summary="unchanged")
    report = _check(conn)
    assert report["fired"] == [] and [e["round"] for e in report["rounds"]] == [2]
    assert _status(conn, tid) == "review"

    # round 3: reaches the limit -> sticky human escalation (triage), ONCE
    kb.reopen_review_task(conn, tid)
    _review_stop(conn, tid, summary="unchanged")
    report = _check(conn)
    assert [e["round"] for e in report["rounds"]] == [3]
    assert len(report["escalated"]) == 1
    assert _status(conn, tid) == "triage"
    assert _events(conn, tid, "watchdog_escalated") == 1
    row = kw.get_watchdog(conn, wd["id"])
    assert row["escalated_at"] is not None

    # sticky: later checks are silent — no re-fire, no re-escalation
    frozen = _snapshot(conn)
    silent = _check(conn)
    assert silent["checked"] == 0
    assert silent["fired"] == [] and silent["rounds"] == [] and silent["escalated"] == []
    assert _snapshot(conn) == frozen
    assert _events(conn, tid, "watchdog_escalated") == 1
    assert _events(conn, tid, "watchdog_fired") == 1
    conn.close()


def test_decided_firing_still_counts_identical_rounds(kanban_home):
    conn = _connect()
    tid = _task(conn, assignee="impl")
    _watch(conn, tid, reviewer="sdlc-review")
    _review_stop(conn, tid, summary="same")
    _check(conn)
    wd_id = kw.list_watchdogs(conn, _board_slug())[0]["id"]
    kw.decide_firing(conn, wd_id, verb="accept", decider="sdlc-review")
    # the reviewer accepted, but the subject keeps bouncing identically:
    # rounds still count and the limit still escalates
    for expected in (2, 3):
        kb.reopen_review_task(conn, tid)
        _review_stop(conn, tid, summary="same")
        report = _check(conn)
        assert [e["round"] for e in report["rounds"]] == [expected]
    assert _status(conn, tid) == "triage"
    conn.close()


# ---------------------------------------------------------------- old cards untouched


def test_check_touches_no_unwatched_cards(kanban_home):
    conn = _connect()
    plain = _task(conn, title="plain running")
    stopped = _task(conn, title="unwatched review")
    _review_stop(conn, stopped)
    blocked = _task(conn, title="unwatched blocked")
    kb.block_task(conn, blocked, reason="waiting", kind="needs_input")
    watched = _task(conn, title="watched")
    _watch(conn, watched)

    frozen = {tid: _snapshot(conn)[tid] for tid in (plain, stopped, blocked)}
    report = _check(conn)
    assert report["checked"] == 1  # only the watched task
    fresh = _snapshot(conn)
    for tid in (plain, stopped, blocked):
        assert fresh[tid] == frozen[tid]
        for kind in ("watchdog_fired", "watchdog_round", "watchdog_escalated",
                     "watchdog_decided"):
            assert _events(conn, tid, kind) == 0
    # the watched task has not stopped: no firing, no events at all
    assert _events(conn, watched, "watchdog_fired") == 0
    conn.close()


def test_dispatcher_flag_off_is_neutral(kanban_home):
    from hermes_cli import kanban_db_dispatch as kdispatch

    conn = _connect()
    tid = _task(conn, assignee="impl")
    _watch(conn, tid, reviewer="sdlc-review")
    kb.block_task(conn, tid, reason="quota", kind="transient")

    # flag OFF (default): the tick never runs the phase — byte-identical
    assert kw.watchdog_tick_enabled() is False
    result = kdispatch._dispatch_once_locked(
        conn, spawn_fn=lambda *a, **k: None, ttl_seconds=60,
        stale_timeout_seconds=0, board=None,
    )
    assert result.watchdog_report is None
    assert kw.list_firings(conn, _board_slug()) == []
    conn.close()


def test_dispatcher_flag_on_runs_the_phase_exactly_once(watchdog_tick_on):
    from hermes_cli import kanban_db_dispatch as kdispatch

    conn = _connect()
    tid = _task(conn, assignee="impl")
    _watch(conn, tid, reviewer="sdlc-review")
    kb.block_task(conn, tid, reason="quota", kind="transient")
    assert kw.watchdog_tick_enabled() is True

    # first armed tick fires; the second armed tick is exactly-once silent
    for expected_fires in (1, 0):
        result = kdispatch._dispatch_once_locked(
            conn, spawn_fn=lambda *a, **k: None, ttl_seconds=60,
            stale_timeout_seconds=0, board=None,
        )
        report = result.watchdog_report
        assert report is not None
        assert len(report["fired"]) == expected_fires
    assert len(kw.list_firings(conn, _board_slug())) == 1
    conn.close()


def test_config_keys_live_through_real_loader(watchdog_on):
    # the enabled flag arms the reader (fixture wrote a real config.yaml)
    assert kw.watchdog_enabled() is True
    assert kw.watchdog_tick_enabled() is False


def test_config_keys_default_off(kanban_home):
    # no config.yaml written: both governance flags stay off (the worker
    # tools stay hidden and the dispatcher tick stays byte-identical)
    assert kw.watchdog_enabled() is False
    assert kw.watchdog_tick_enabled() is False


# ---------------------------------------------------------------- CLI surface


def _cli(args: argparse.Namespace) -> int:
    return kw.dispatch_watchdog(args)


def test_cli_read_create_apply_paths(kanban_home, capsys):
    conn = _connect()
    tid = _task(conn, assignee="impl")
    conn.close()

    # create
    rc = _cli(argparse.Namespace(watchdog_action="create", task=tid,
                                 reviewer="sdlc-review",
                                 instructions="verify the receipt",
                                 json=True, board=None))
    assert rc == 0, capsys.readouterr().out
    wd = json.loads(capsys.readouterr().out)
    assert wd["reviewer"] == "sdlc-review"

    # list + show (task-id reference resolves to the active watchdog)
    rc = _cli(argparse.Namespace(watchdog_action="list", status=None, json=True,
                                 board=None))
    assert rc == 0
    listing = json.loads(capsys.readouterr().out)
    assert [w["task_id"] for w in listing] == [tid]
    rc = _cli(argparse.Namespace(watchdog_action="show", watchdog_id=tid, json=True,
                                 board=None))
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["watchdog"]["id"] == wd["id"]

    # check: the stop fires
    conn = _connect()
    _review_stop(conn, tid)
    conn.close()
    rc = _cli(argparse.Namespace(watchdog_action="check", task=None, json=True,
                                 board=None))
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert [e["task_id"] for e in report["fired"]] == [tid]

    # decide (human CLI surface: any non-self profile)
    rc = _cli(argparse.Namespace(watchdog_action="decide", watchdog_id=wd["id"],
                                 verb="accept", note=None, assignee=None,
                                 json=True, board=None))
    assert rc == 0
    decided = json.loads(capsys.readouterr().out)
    assert decided["firing"]["outcome"] == "accepted"

    # rm retires; the row stays auditable
    rc = _cli(argparse.Namespace(watchdog_action="rm", watchdog_id=wd["id"],
                                 json=True, board=None))
    assert rc == 0
    retired = json.loads(capsys.readouterr().out)
    assert retired["status"] == "retired"


def test_cli_create_refuses_self_review(kanban_home, capsys):
    conn = _connect()
    tid = _task(conn, assignee="alice")
    conn.close()
    rc = _cli(argparse.Namespace(watchdog_action="create", task=tid, reviewer="alice",
                                 instructions=None, json=True, board=None))
    assert rc == 2
    assert "no self-review" in capsys.readouterr().out
