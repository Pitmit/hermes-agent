"""Kanban governance stage 4: the blocked inbox.

Spec: docs/kanban-governance-spec.md §6 (Stufe 4). Task card t_35462e45.
These tests prove the stage's contract:

* the severity ladder: base severity per block_kind escalated by stopped age
  relative to the effective SLA (board default, per-task override, config
  override) — including the per-task ``block_sla_hours`` override escalating
  EARLIER (spec test e);
* the projection is deterministically sorted (severity desc, age desc, ref
  asc) and BOUNDED; invalid limits fail closed;
* ``action_owner`` is derived per source/kind;
* ``stopped_age`` is measured from the LAST ``blocked`` event (a re-block
  restarts the clock) and reported as ``age_seconds``/``blocked_since``;
* the inbox is a PURE READ: no task_events, no approval_events, no status
  changes — sticky human/credential/safety gates stay sticky across
  dispatcher ticks (binary acceptance criteria 1 and 3);
* pending approvals and open reviews appear in the SAME query, severity
  first (spec test d), every row carrying severity/action_owner/age_seconds/
  source (binary acceptance criterion 2);
* ``set_block_sla`` is the only write: audited (block_sla_set), fail-closed
  on invalid hours, never a status change;
* a legacy board DB (pre-block_sla_hours) migrates additively and serves;
* the worker tools exist and the read tool appends nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_approvals as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_inbox as ki


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Deterministic board/worker fencing even when the test process is a
    # dispatched kanban worker itself.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def approvals_on(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  approvals:\n    enabled: true\n"
    )
    return kanban_home


def _connect():
    return kbc.connect()


def _board() -> str:
    return kb.get_current_board() or kb.DEFAULT_BOARD


_NOW = int(time.time())  # projection clock: real now, so live-created events
# (review requests, which the kernel stamps with time.time()) age correctly.


def _blocked_task(conn, kind, *, reason=None, age_hours=None, title=None, assignee="alice"):
    """Create + claim + block a task with a typed kind; optionally age its
    ``blocked`` event to ``age_hours`` hours before ``_NOW``."""
    tid = kb.create_task(conn, title=title or f"task {kind}", assignee=assignee)
    kb.claim_task(conn, tid)
    assert kb.block_task(
        conn, tid, reason=reason, kind=kind,
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    assert kb.get_task(conn, tid).status == "blocked"
    if age_hours is not None:
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
            (_NOW - int(age_hours * 3600), tid),
        )
    return tid


def _inbox(conn, **kw):
    kw.setdefault("now", _NOW)
    return ki.inbox_rows(conn, board=_board(), **kw)


def _row(rows, ref):
    matches = [r for r in rows if r["ref"] == ref]
    assert matches, f"no inbox row for {ref} in {[r['ref'] for r in rows]}"
    return matches[0]


def _event_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------- severity ladder


def test_severity_base_mapping_and_age_escalation(kanban_home):
    with _connect() as conn:
        fresh = _blocked_task(conn, "needs_input", age_hours=1)
        mid = _blocked_task(conn, "needs_input", age_hours=3 * 24)
        old = _blocked_task(conn, "needs_input", age_hours=7 * 24)
        cap = _blocked_task(conn, "capability", age_hours=1)
        cap_old = _blocked_task(conn, "capability", age_hours=7 * 24)
        trans = _blocked_task(conn, "transient", age_hours=1)
        rows = _inbox(conn)
        # Board default SLA is 24h: <2x keeps base, 2-6x escalates one step,
        # >6x escalates two steps (capped at critical).
        assert _row(rows, fresh)["severity"] == "medium"
        assert _row(rows, mid)["severity"] == "high"
        assert _row(rows, old)["severity"] == "critical"
        assert _row(rows, cap)["severity"] == "high"
        assert _row(rows, cap_old)["severity"] == "critical"
        assert _row(rows, trans)["severity"] == "medium"


def test_untyped_legacy_block_defaults_to_medium(kanban_home):
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=1)
        conn.execute("UPDATE tasks SET block_kind = NULL WHERE id = ?", (tid,))
        row = _row(_inbox(conn), tid)
        assert row["severity"] == "medium"
        assert row["block_kind"] is None
        assert row["action_owner"] == "human"


def test_per_task_sla_override_escalates_earlier(kanban_home):
    with _connect() as conn:
        plain = _blocked_task(conn, "needs_input", age_hours=3, title="plain")
        fast = _blocked_task(conn, "needs_input", age_hours=3, title="override")
        ki.set_block_sla(conn, fast, 1)
        rows = _inbox(conn)
        assert _row(rows, plain)["severity"] == "medium"   # 3h vs 24h board SLA
        assert _row(rows, fast)["severity"] == "high"      # 3h vs 1h override
        assert _row(rows, fast)["sla_override"] == 1.0
        assert _row(rows, fast)["sla_hours"] == 1.0
        assert _row(rows, plain)["sla_override"] is None
        assert _row(rows, plain)["sla_hours"] == 24.0


def test_board_sla_config_override(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  diagnostics:\n    blocked_stale_hours: 1\n"
    )
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=3)
        # 3h against a 1h board SLA is already >= 2x -> one escalation step.
        assert _row(_inbox(conn), tid)["severity"] == "high"


def test_approval_rows_age_against_approval_sla(approvals_on):
    with _connect() as conn:
        # Two DISTINCT subjects: identical (subject, requester) requests dedup.
        subject_a = kb.create_task(conn, title="subject a", assignee="alice")
        subject_b = kb.create_task(conn, title="subject b", assignee="alice")
        fresh = ka.request_approval(
            conn, board=_board(), type="action", subject_kind="task",
            subject_ref=subject_a, requester="alice", now=_NOW - 3600,
        )["id"]
        stale = ka.request_approval(
            conn, board=_board(), type="action", subject_kind="task",
            subject_ref=subject_b, requester="alice", now=_NOW - 4 * 24 * 3600,
        )["id"]
        rows = _inbox(conn)
        assert _row(rows, fresh)["severity"] == "medium"   # 1h vs 48h SLA
        assert _row(rows, stale)["severity"] == "high"     # 4d >= 2x 48h


def test_review_rows_carry_reviewer_owner(kanban_home):
    with _connect() as conn:
        tid = kb.create_task(conn, title="review me", assignee="bob")
        kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="please review", force=True)
        assert kb.get_task(conn, tid).status == "review"
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? "
            "AND kind = 'review_requested'", (_NOW - 3600, tid),
        )
        row = _row(_inbox(conn), tid)
        assert row["source"] == "review"
        assert row["action_owner"] == "reviewer"
        assert row["age_seconds"] == 3600
        assert row["severity"] == "medium"


# ------------------------------------------------- ordering and bounding


def test_deterministic_sort_and_bounded_projection(kanban_home):
    with _connect() as conn:
        cap_old = _blocked_task(conn, "capability", age_hours=10 * 24, title="cap old")
        ni_old = _blocked_task(conn, "needs_input", age_hours=9 * 24, title="ni old")
        ni_mid = _blocked_task(conn, "needs_input", age_hours=3 * 24, title="ni mid")
        ni_fresh = _blocked_task(conn, "needs_input", age_hours=1, title="ni fresh")
        # A blocked card with NO blocked event (legacy breaker-parked).
        legacy = kb.create_task(conn, title="legacy parked", assignee="alice")
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (legacy,),
        )
        rows = _inbox(conn)
        assert [r["ref"] for r in rows] == [cap_old, ni_old, ni_mid, ni_fresh, legacy]
        # Bounded: the projection is a prefix of the full order.
        assert [r["ref"] for r in _inbox(conn, limit=2)] == [cap_old, ni_old]


def test_limit_and_filters_fail_closed(kanban_home):
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=1)
        for bad in (0, -1, ki.INBOX_MAX_LIMIT + 1, "many", True, None):
            with pytest.raises(ValueError):
                _inbox(conn, limit=bad)
        for bad_severity in ("severe", "warning", "error", "info", ""):
            with pytest.raises(ValueError):
                _inbox(conn, severity=bad_severity)
        with pytest.raises(ValueError):
            _inbox(conn, source="queue")
        with pytest.raises(ValueError):
            _inbox(conn, block_kind="urgent")
        # Valid filters keep working and stay at-or-above semantics.
        rows = _inbox(conn, severity="medium", source="blocked", block_kind="needs_input")
        assert [r["ref"] for r in rows] == [tid]
        # At-or-above: a high threshold drops the fresh medium row.
        assert _inbox(conn, severity="high") == []


def test_sort_is_stable_across_equal_rows(kanban_home):
    with _connect() as conn:
        a = _blocked_task(conn, "needs_input", age_hours=1, title="a")
        b = _blocked_task(conn, "needs_input", age_hours=1, title="b")
        refs = sorted([a, b])
        assert [r["ref"] for r in _inbox(conn)] == refs  # ref asc tiebreak


# ------------------------------------------------------------ action owner


def test_action_owner_mapping_per_kind(kanban_home):
    with _connect() as conn:
        ni = _blocked_task(conn, "needs_input", age_hours=1)
        cap = _blocked_task(conn, "capability", age_hours=1)
        trans = _blocked_task(conn, "transient", age_hours=1)
        rows = {r["ref"]: r for r in _inbox(conn)}
        assert rows[ni]["action_owner"] == "human"
        assert rows[cap]["action_owner"] == "human-ops"
        assert rows[trans]["action_owner"] == "dispatcher-retry"
        # Block reason survives into the projection.
        assert rows[ni]["block_reason"] is None
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'blocked'",
            (json.dumps({"kind": "needs_input", "reason": "approval:ap_1"}), ni),
        )
        assert _row(_inbox(conn), ni)["block_reason"] == "approval:ap_1"


def test_dependency_waits_never_enter_the_blocked_inbox(kanban_home):
    """``dependency`` blocks wait in ``todo`` for parent gating (block_task
    semantics, unchanged): they must NOT surface as blocked rows. A dependency
    block with no open parent is even re-kinded to sticky needs_input."""
    with _connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="alice")
        kb.link_tasks(conn, parent, child)
        kb.claim_task(conn, child)
        kb.block_task(
            conn, child, kind="dependency", reason="parent open",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        # Routed to todo (dependency_wait), never blocked.
        assert kb.get_task(conn, child).status == "todo"
        assert all(r["ref"] != child for r in _inbox(conn))
        # No open parent -> re-kinded to sticky needs_input -> blocked.
        orphan = _blocked_task(conn, "dependency", reason="no parent", age_hours=1)
        assert kb.get_task(conn, orphan).block_kind == "needs_input"
        row = _row(_inbox(conn), orphan)
        assert row["action_owner"] == "human"


# -------------------------------------------------------------- stopped age


def test_stopped_age_measures_from_last_blocked_event(kanban_home):
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=10 * 24)
        row = _row(_inbox(conn), tid)
        assert row["blocked_since"] == _NOW - 10 * 24 * 3600
        assert row["age_seconds"] == 10 * 24 * 3600
        assert row["stopped_age"] == row["age_seconds"]
        # Human unblocks, the task is re-blocked: the NEW blocked event
        # restarts the clock (age is measured from the latest event only).
        # A DIFFERENT kind avoids the recurrence breaker (a same-kind re-block
        # would correctly route the card to triage, not blocked).
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status != "blocked"
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid, kind="transient", reason="again",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"
        row = _row(_inbox(conn), tid)
        assert row["age_seconds"] < 3600
        assert row["blocked_since"] > _NOW - 3600


# ----------------------------------------------------- pure read invariant


def test_inbox_read_appends_no_events(approvals_on):
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=1)
        ap = ka.request_approval(
            conn, board=_board(), type="action", subject_kind="task",
            subject_ref=tid, requester="alice",
        )["id"]
        before = (_event_count(conn, "task_events"), _event_count(conn, "approval_events"))
        for _ in range(3):
            rows = _inbox(conn)
        assert _row(rows, tid)["source"] == "blocked"
        assert _row(rows, ap)["source"] == "approval"
        after = (_event_count(conn, "task_events"), _event_count(conn, "approval_events"))
        assert after == before


def test_inbox_does_not_mutate_statuses_or_gates(kanban_home):
    with _connect() as conn:
        ni = _blocked_task(conn, "needs_input", age_hours=30 * 24)
        cap = _blocked_task(conn, "capability", age_hours=1)
        before = {t.id: t.status for t in kb.list_tasks(conn)}
        _inbox(conn)  # 3 reads, like 3 dispatcher periods with an inbox poll
        _inbox(conn)
        _inbox(conn)
        after = {t.id: t.status for t in kb.list_tasks(conn)}
        assert after == before
        assert after[ni] == "blocked" and after[cap] == "blocked"


def test_sticky_gates_survive_dispatcher_ticks(kanban_home):
    """Binary acceptance criterion 1: N>=3 dispatcher ticks (the promotion
    pass the tick runs) never move needs_input/capability blocks."""
    with _connect() as conn:
        ni = _blocked_task(conn, "needs_input", reason="human needed", age_hours=1)
        cap = _blocked_task(conn, "capability", reason="no rights", age_hours=1)
        for tick in range(4):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, f"tick {tick} auto-promoted a sticky gate"
            statuses = {t.id: t.status for t in kb.list_tasks(conn)}
            assert statuses[ni] == "blocked"
            assert statuses[cap] == "blocked"


# ---------------------------------------------------- set_block_sla writes


def test_set_block_sla_roundtrip_with_audit(kanban_home):
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=1)
        assert kb.get_task(conn, tid).block_sla_hours is None
        result = ki.set_block_sla(conn, tid, 2)
        assert result == {"task_id": tid, "block_sla_hours": 2.0,
                          "previous": None, "cleared": False}
        assert kb.get_task(conn, tid).block_sla_hours == 2.0
        events = [e for e in kb.list_events(conn, tid) if e.kind == "block_sla_set"]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["hours"] == 2.0 and payload["previous"] is None
        # The SLA write never touches status.
        assert kb.get_task(conn, tid).status == "blocked"
        # Update + clear are audited too.
        ki.set_block_sla(conn, tid, 6)
        cleared = ki.set_block_sla(conn, tid, None, clear=True)
        assert cleared["cleared"] is True
        assert kb.get_task(conn, tid).block_sla_hours is None
        events = [e for e in kb.list_events(conn, tid) if e.kind == "block_sla_set"]
        assert [e.payload["hours"] for e in events] == [2.0, 6.0, None]


def test_set_block_sla_fail_closed_on_invalid_values(kanban_home):
    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=1)
        for bad in (0, -1, "2", True, False, None, 1e12):
            with pytest.raises(ValueError):
                ki.set_block_sla(conn, tid, bad)
        assert kb.get_task(conn, tid).block_sla_hours is None
        # No audit noise from refused writes.
        assert [e for e in kb.list_events(conn, tid) if e.kind == "block_sla_set"] == []
        # Unknown task fails closed too.
        with pytest.raises(LookupError):
            ki.set_block_sla(conn, "t_nope", 2)


# ------------------------------------------------------- legacy DB migration


def test_legacy_db_migrates_block_sla_hours_additively(kanban_home):
    conn = _connect()
    tid = _blocked_task(conn, "needs_input", age_hours=1)
    conn.close()

    # Simulate a pre-stage-4 board: the additive column is absent.
    path = kb.kanban_db_path()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE tasks DROP COLUMN block_sla_hours")
    raw.commit()
    raw.close()
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))

    # The migrated board re-adds the column, serves the inbox with safe
    # defaults (NULL -> board default SLA, untyped base severity) and accepts
    # a per-task override.
    with _connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "block_sla_hours" in cols
        rows = _inbox(conn)
        row = _row(rows, tid)
        assert row["severity"] == "medium"
        assert row["sla_hours"] == 24.0
        ki.set_block_sla(conn, tid, 5)
        assert _row(_inbox(conn), tid)["sla_override"] == 5.0


# --------------------------------------------------------- one-query merge


def test_all_three_sources_in_one_query_severity_first(approvals_on):
    with _connect() as conn:
        blocked = _blocked_task(conn, "needs_input", age_hours=3 * 24)  # high
        subject = kb.create_task(conn, title="subject", assignee="alice")
        ap = ka.request_approval(
            conn, board=_board(), type="action", subject_kind="task",
            subject_ref=subject, requester="alice", now=_NOW - 3600,
        )["id"]
        review = kb.create_task(conn, title="review me", assignee="bob")
        kb.claim_task(conn, review)
        kb.request_review(conn, review, summary="please", force=True)
        rows = _inbox(conn)
        sources = {r["source"] for r in rows}
        assert sources == {"blocked", "approval", "review"}
        # Severity-first: the high-severity blocked row leads the mediums.
        assert rows[0]["ref"] == blocked
        assert {rows[1]["ref"], rows[2]["ref"]} == {ap, review}
        # Binary acceptance criterion 2: every row carries the contract keys.
        for r in rows:
            assert r["severity"] in ki.VALID_SEVERITIES
            assert r["action_owner"]
            assert r["source"] in ki.VALID_INBOX_SOURCES
            assert isinstance(r["age_seconds"], int)


# ------------------------------------------------------------ CLI surface


def test_cli_inbox_and_block_sla_roundtrip(kanban_home, capsys):
    from hermes_cli.kanban import _cmd_block_sla, _cmd_inbox
    from hermes_cli.kanban_parser import build_parser

    with _connect() as conn:
        tid = _blocked_task(conn, "needs_input", age_hours=1)

    wrap = argparse.ArgumentParser(prog="kanban-wrap")
    parser = build_parser(wrap.add_subparsers(dest="_top"))

    def run(argv):
        args = parser.parse_args(argv)
        return args

    args = run(["inbox", "--json"])
    assert args.kanban_action == "inbox"
    rc = _cmd_inbox(args)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["board"] == _board()
    refs = {r["ref"] for r in payload["rows"]}
    assert tid in refs

    # Human-mode render (no --json) prints the severity table.
    rc = _cmd_inbox(run(["inbox"]))
    assert rc == 0
    assert "severity" in capsys.readouterr().out

    # block-sla: show -> set -> show -> clear.
    rc = _cmd_block_sla(run(["block-sla", tid]))
    assert rc == 0 and "board default" in capsys.readouterr().out
    rc = _cmd_block_sla(run(["block-sla", tid, "--hours", "2", "--json"]))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["block_sla_hours"] == 2.0
    with _connect() as conn:
        assert kb.get_task(conn, tid).block_sla_hours == 2.0
    rc = _cmd_block_sla(run(["block-sla", tid, "--clear"]))
    assert rc == 0 and "cleared" in capsys.readouterr().out
    with _connect() as conn:
        assert kb.get_task(conn, tid).block_sla_hours is None

    # Fail-closed CLI paths: exit != 0, no state change.
    rc = _cmd_block_sla(run(["block-sla", tid, "--hours", "0"]))
    assert rc == 1 and "must be" in capsys.readouterr().out
    rc = _cmd_inbox(run(["inbox", "--limit", "0"]))
    assert rc == 1 and "limit" in capsys.readouterr().out
    rc = _cmd_block_sla(run(["block-sla", "t_missing", "--hours", "2"]))
    assert rc == 1
    with _connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"

    # Invalid enum values never reach the handler (argparse choices).
    with pytest.raises(SystemExit):
        parser.parse_args(["inbox", "--severity", "severe"])
    with pytest.raises(SystemExit):
        parser.parse_args(["inbox", "--source", "queue"])
    with pytest.raises(SystemExit):
        parser.parse_args(["inbox", "--kind", "urgent"])


def test_config_defaults_register_the_threshold_keys(kanban_home):
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    diag = DEFAULT_CONFIG["kanban"]["diagnostics"]
    assert diag["blocked_stale_hours"] == 24
    assert diag["approval_stale_hours"] == 48


# ------------------------------------------------------------- tool surface


def test_tools_registered_and_read_tool_appends_nothing(kanban_home):
    from tools import kanban_tools as kt
    from tools.registry import registry

    names = [row[0] for row in kt._TOOLS]
    assert "kanban_inbox" in names
    assert "kanban_set_block_sla" in names
    assert registry.get_schema("kanban_inbox")["name"] == "kanban_inbox"
    assert registry.get_schema("kanban_set_block_sla")["name"] == "kanban_set_block_sla"
    # The inbox is worker-readable; it is NOT an unblock/release verb.
    for forbidden in ("kanban_unblock_all", "kanban_auto_release", "kanban_release_gates"):
        assert forbidden not in names

    conn = _connect()
    tid = _blocked_task(conn, "needs_input", age_hours=1)
    conn.close()
    conn = _connect()
    before = _event_count(conn, "task_events")
    conn.close()
    payload = json.loads(kt._handle_inbox({}))
    assert payload["ok"] is True
    assert any(r["ref"] == tid for r in payload["rows"])
    conn = _connect()
    assert _event_count(conn, "task_events") == before
    conn.close()


def test_set_block_sla_tool_roundtrip_and_fail_closed(kanban_home):
    from tools import kanban_tools as kt

    conn = _connect()
    tid = _blocked_task(conn, "needs_input", age_hours=1)
    conn.close()
    payload = json.loads(kt._handle_set_block_sla({"task_id": tid, "hours": 3}))
    assert payload["ok"] is True and payload["block_sla_hours"] == 3.0
    conn = _connect()
    assert kb.get_task(conn, tid).block_sla_hours == 3.0
    assert kb.get_task(conn, tid).status == "blocked"
    conn.close()
    # Fail-closed tool paths return the bounded error body, never a traceback.
    for bad_args in ({"task_id": tid, "hours": 0}, {"task_id": tid},
                     {"task_id": "t_missing", "hours": 1}):
        payload = json.loads(kt._handle_set_block_sla(bad_args))
        assert "error" in payload
    payload = json.loads(kt._handle_set_block_sla({"task_id": tid, "clear": True}))
    assert payload["ok"] is True and payload["cleared"] is True
