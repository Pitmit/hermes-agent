"""Kanban approval governance stage 2: generic human-approval engine.

Spec: docs/kanban-governance-spec.md §4 (Stufe 2). These tests prove the
stage's contract:

* the COMPLETE status matrix — every decision from every open state lands in
  the right status, terminal states refuse further decisions without
  mutating anything;
* self-approval is fail-closed (requester == approver -> PermissionError);
* drift invalidates: a decision on a changed subject fails, the open request
  becomes 'invalidated' with EXACTLY ONE approval_invalidated event, and a
  second attempt adds no second event;
* an identical request is deduplicated (one row, one event) — the "lost tool
  response" is read-back-able: re-requesting returns the same id, and
  show/list return the live state;
* a legacy board DB (pre-approvals) migrates additively and still serves;
* approving releases EXACTLY the tasks bound by 'approval:<id>' (exact-reason
  contract, Vorher/Nachher-Statusdiff) with the shared unblock semantics;
* workers can never decide: no decide tool exists in the registry, and the
  kernel refuses dispatched-worker contexts; the request tool is flag-gated;
* board isolation and server-side fingerprint determinism (two identical
  subjects -> same hash; a change -> different hash);
* every status transition appends exactly one audit event; pure reads of
  non-drifted approvals append none;
* the existing task-review loop stays untouched (separate suites stay green).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_approvals as ka
from hermes_cli import kanban_cost as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The test process may itself be a dispatched kanban worker — strip its
    # board/task env so board resolution AND the worker fence are deterministic.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def approvals_on(kanban_home):
    """``kanban.approvals.enabled: true`` through the real config loader."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  approvals:\n    enabled: true\n"
    )
    return kanban_home


# ---------------------------------------------------------------- helpers


def _board_slug() -> str:
    return kb.get_current_board() or kb.DEFAULT_BOARD


def _connect():
    return kbc.connect()


def _request(conn, *, subject_kind="task", subject_ref=None, requester="alice",
             type="action", **kw):
    """Create a task subject by default and request an approval over it."""
    if subject_ref is None:
        subject_ref = kb.create_task(conn, title="subject", assignee="alice")
    return ka.request_approval(
        conn, board=_board_slug(), type=type, subject_kind=subject_kind,
        subject_ref=subject_ref, requester=requester, **kw,
    )


def _events(conn, approval_id: str, kind: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM approval_events WHERE approval_id = ? AND kind = ?",
        (approval_id, kind),
    ).fetchone()[0])


def _status(conn, approval_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM approvals WHERE id = ?", (approval_id,),
    ).fetchone()
    assert row is not None
    return row["status"]


def _decide(conn, approval_id, decision, approver="peter", **kw):
    return ka.decide_approval(
        conn, approval_id, decision=decision, approver=approver, via="cli", **kw,
    )


# ---------------------------------------------------------------- migration


def test_legacy_db_creates_approvals_tables_and_serves(kanban_home):
    conn = _connect()
    tid = kb.create_task(conn, title="history", assignee="alice")
    conn.close()

    # Simulate a pre-governance board: stage-2 tables absent.
    path = kb.kanban_db_path()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE IF EXISTS approvals")
    raw.execute("DROP TABLE IF EXISTS approval_events")
    raw.commit()
    raw.close()
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))

    # The migrated board re-creates the tables additively and still serves
    # reads (criterion: `hermes kanban list`) AND approval requests.
    conn = _connect()
    assert any(t.id == tid for t in kb.list_tasks(conn))
    result = _request(conn, subject_ref=tid)
    assert result["status"] == "pending"
    assert _status(conn, result["id"]) == "pending"
    # Idempotent: a second init cycle changes nothing.
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    conn.close()
    conn = _connect()
    assert _status(conn, result["id"]) == "pending"
    assert _events(conn, result["id"], "approval_requested") == 1
    conn.close()


# ---------------------------------------------------------------- lifecycle


@pytest.mark.parametrize("decision,expected", [
    ("approve", "approved"),
    ("reject", "rejected"),
    ("revise", "revision_requested"),
])
def test_status_matrix_from_pending(kanban_home, decision, expected):
    conn = _connect()
    result = _request(conn)
    ap = result["id"]
    assert result["status"] == "pending"
    decided = _decide(conn, ap, decision)
    assert decided["status"] == expected
    assert decided["approver"] == "peter"
    assert decided["decided_via"] == "cli"
    assert decided["decided_at"] is not None
    # Exactly one requested + one decided event — the audit is complete and
    # never duplicated.
    assert _events(conn, ap, "approval_requested") == 1
    assert _events(conn, ap, "approval_decided") == 1
    assert _events(conn, ap, "approval_invalidated") == 0
    conn.close()


@pytest.mark.parametrize("decision,expected", [
    ("approve", "approved"),
    ("reject", "rejected"),
    # revise keeps the request open — a second revise is a legitimate
    # still-open decision (round 2 of the same human gate).
    ("revise", "revision_requested"),
])
def test_status_matrix_from_revision_requested(kanban_home, decision, expected):
    conn = _connect()
    ap = _request(conn)["id"]
    _decide(conn, ap, "revise")
    assert _status(conn, ap) == "revision_requested"
    decided = _decide(conn, ap, decision)
    assert decided["status"] == expected
    assert _events(conn, ap, "approval_decided") == 2
    conn.close()


@pytest.mark.parametrize("terminal_decision", ["approve", "reject"])
@pytest.mark.parametrize("decision", ["approve", "reject", "revise"])
def test_status_matrix_terminal_states_refuse_every_decision(
    kanban_home, terminal_decision, decision,
):
    conn = _connect()
    ap = _request(conn)["id"]
    _decide(conn, ap, terminal_decision)
    before = _status(conn, ap)
    with pytest.raises(ka.ApprovalStateError):
        _decide(conn, ap, decision)
    assert _status(conn, ap) == before
    # Refused decisions are no-ops: no extra event, no state change.
    assert _events(conn, ap, "approval_decided") == 1
    assert _events(conn, ap, "approval_invalidated") == 0
    conn.close()


# ---------------------------------------------------------------- fences


def test_self_approval_is_fail_closed(kanban_home):
    conn = _connect()
    ap = _request(conn, requester="alice")["id"]
    with pytest.raises(PermissionError, match="self-approval"):
        _decide(conn, ap, "approve", approver="alice")
    assert _status(conn, ap) == "pending"
    assert _events(conn, ap, "approval_decided") == 0
    # A DIFFERENT profile may decide the same request.
    decided = _decide(conn, ap, "approve", approver="peter")
    assert decided["status"] == "approved"
    conn.close()


def test_worker_context_cannot_decide(kanban_home, monkeypatch):
    conn = _connect()
    ap = _request(conn)["id"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_some_task")
    with pytest.raises(PermissionError, match="workers cannot decide"):
        _decide(conn, ap, "approve")
    assert _status(conn, ap) == "pending"
    assert _events(conn, ap, "approval_decided") == 0
    conn.close()


def test_no_decide_tool_exists_in_the_registry(kanban_home):
    """Workers can ask, never decide: no approve/reject/revise tool is
    registered on any toolset (schema-contract, not a count assert)."""
    from tools import kanban_tools as kt
    from tools.registry import registry

    names = [row[0] for row in kt._TOOLS]
    assert "kanban_approval_request" in names
    for forbidden in ("kanban_approval_approve", "kanban_approval_reject",
                      "kanban_approval_revise", "kanban_approval_decide",
                      "kanban_approval_list", "kanban_approval_show"):
        assert forbidden not in names
        # get_schema returns None for unknown tools (never raises): the decide
        # verbs are simply NOT REGISTERED anywhere.
        assert registry.get_schema(forbidden) is None


def test_request_tool_is_flag_gated(kanban_home, approvals_on, monkeypatch):
    from tools import kanban_tools as kt

    # Off (default): the worker surface stays hidden even for a kanban worker.
    (kanban_home / "config.yaml").unlink()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_some_task")
    assert kt._check_kanban_approvals_mode() is False

    # On: a dispatched worker sees the request tool.
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  approvals:\n    enabled: true\n"
    )
    assert kt._check_kanban_approvals_mode() is True

    # DEFAULT_CONFIG registers the key with default False (no config file).
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["approvals"]["enabled"] is False


def test_request_tool_files_request_only(approvals_on):
    from tools import kanban_tools as kt

    conn = _connect()
    tid = kb.create_task(conn, title="subject", assignee="alice")
    conn.close()
    payload = json.loads(kt._handle_approval_request({
        "type": "action", "subject_kind": "task", "subject_ref": tid,
        "note": "needs a human",
    }))
    assert payload["ok"] is True
    assert payload["status"] == "pending"
    assert payload["type"] == "action"
    assert payload["subject_id"] == tid
    # Requester is the persisted identity (never a caller-supplied forge).
    assert payload["requester"]
    conn = _connect()
    assert _status(conn, payload["id"]) == "pending"
    conn.close()


# ---------------------------------------------------------------- idempotency


def test_identical_request_is_deduplicated(kanban_home):
    conn = _connect()
    tid = kb.create_task(conn, title="fixed subject", assignee="alice")
    first = _request(conn, subject_ref=tid, requester="alice", note="first")
    assert first["deduplicated"] is False
    # Identical request (same subject, same requester) — even with a
    # different note — returns the SAME open approval.
    second = _request(conn, subject_ref=tid, requester="alice", note="lost the first response")
    assert second["deduplicated"] is True
    assert second["id"] == first["id"]
    assert int(conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == 1
    assert _events(conn, first["id"], "approval_requested") == 1
    conn.close()


def test_lost_response_is_read_back_able(kanban_home):
    """The worker lost the tool response: re-request returns the same id and
    show/list return the live state — no orphan duplicate row."""
    conn = _connect()
    tid = kb.create_task(conn, title="fixed subject", assignee="alice")
    first = _request(conn, subject_ref=tid, requester="alice")
    # ... response lost; re-request:
    again = _request(conn, subject_ref=tid, requester="alice")
    assert again["id"] == first["id"]
    # ... or read back directly:
    row = ka.get_approval(conn, first["id"])
    assert row["id"] == first["id"]
    assert row["status"] == "pending"
    rows = ka.list_approvals(conn, _board_slug())
    assert [r["id"] for r in rows] == [first["id"]]
    assert int(conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == 1
    conn.close()


def test_dedup_scopes_to_open_requests_and_requester(kanban_home):
    conn = _connect()
    tid = kb.create_task(conn, title="subject", assignee="alice")
    alice = ka.request_approval(
        conn, board=_board_slug(), type="action", subject_kind="task",
        subject_ref=tid, requester="alice",
    )
    # A different requester gets their own request for the same subject.
    bob = _request(conn, subject_ref=tid, requester="bob")
    assert bob["id"] != alice["id"]
    assert int(conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == 2
    # After a decision, an identical re-request is a NEW request (the old one
    # is no longer open).
    _decide(conn, alice["id"], "reject")
    fresh = _request(conn, subject_ref=tid, requester="alice")
    assert fresh["deduplicated"] is False
    assert fresh["id"] != alice["id"]
    conn.close()


# ---------------------------------------------------------------- drift


def test_drift_invalidates_and_second_decide_is_refused(kanban_home):
    conn = _connect()
    tid = kb.create_task(conn, title="subject", body="v1", assignee="alice")
    ap = _request(conn, subject_ref=tid)["id"]
    # The subject changes after the request:
    conn.execute("UPDATE tasks SET body = 'v2' WHERE id = ?", (tid,))
    with pytest.raises(ka.ApprovalStateError, match="invalidated"):
        _decide(conn, ap, "approve")
    assert _status(conn, ap) == "invalidated"
    row = conn.execute(
        "SELECT invalidation_reason FROM approvals WHERE id = ?", (ap,),
    ).fetchone()
    assert row["invalidation_reason"] == "subject_drift"
    # Exactly ONE invalidation event; the refused decision left no decided
    # event and a second attempt adds no second invalidation.
    assert _events(conn, ap, "approval_invalidated") == 1
    assert _events(conn, ap, "approval_decided") == 0
    with pytest.raises(ka.ApprovalStateError):
        _decide(conn, ap, "approve")
    assert _events(conn, ap, "approval_invalidated") == 1
    conn.close()


def test_reads_invalidate_drifted_open_requests(kanban_home, tmp_path):
    conn = _connect()
    tid = kb.create_task(conn, title="subject", body="v1", assignee="alice")
    ap = _request(conn, subject_ref=tid)["id"]
    conn.execute("UPDATE tasks SET body = 'v2' WHERE id = ?", (tid,))

    # list (a status read) invalidates the drifted request.
    rows = ka.list_approvals(conn, _board_slug())
    assert rows[0]["status"] == "invalidated"
    assert rows[0]["invalidation_reason"] == "subject_drift"
    assert _events(conn, ap, "approval_invalidated") == 1

    # A document whose file vanished is unresolvable -> invalidated too.
    doc = tmp_path / "plan.md"
    doc.write_text("strategy v1")
    doc_ap = _request(
        conn, type="strategy", subject_kind="document", subject_ref=str(doc),
        requester="alice",
    )["id"]
    doc.unlink()
    row = ka.get_approval(conn, doc_ap)
    assert row["status"] == "invalidated"
    assert row["invalidation_reason"] == "subject_unresolvable"
    assert _events(conn, doc_ap, "approval_invalidated") == 1
    conn.close()


def test_pure_reads_of_healthy_approvals_append_no_events(kanban_home):
    conn = _connect()
    ap = _request(conn)["id"]
    before = int(conn.execute(
        "SELECT COUNT(*) FROM approval_events").fetchone()[0])
    ka.get_approval(conn, ap)
    ka.list_approvals(conn, _board_slug())
    after = int(conn.execute(
        "SELECT COUNT(*) FROM approval_events").fetchone()[0])
    assert before == after
    conn.close()


# ---------------------------------------------------------------- fingerprints


def test_task_fingerprint_is_deterministic_and_drift_sensitive(kanban_home):
    conn = _connect()
    # Deterministic: the SAME subject hashes the same on every resolution
    # (the task id is part of the subject — two different tasks are two
    # different subjects even with equal text).
    tid = kb.create_task(conn, title="same", body="same", assignee="a")
    _, fp1 = ka.resolve_subject(conn, board=_board_slug(), subject_kind="task", subject_ref=tid)
    _, fp2 = ka.resolve_subject(conn, board=_board_slug(), subject_kind="task", subject_ref=tid)
    assert fp1 == fp2
    conn.execute("UPDATE tasks SET title = 'changed' WHERE id = ?", (tid,))
    _, fp1b = ka.resolve_subject(conn, board=_board_slug(), subject_kind="task", subject_ref=tid)
    assert fp1b != fp1


def test_release_plan_fingerprint_ignores_key_order(kanban_home):
    conn = _connect()
    _, fp1 = ka.resolve_subject(
        conn, board=_board_slug(), subject_kind="release_plan",
        subject_ref='{"commit": "abc", "artifacts": ["x"]}',
    )
    _, fp2 = ka.resolve_subject(
        conn, board=_board_slug(), subject_kind="release_plan",
        subject_ref='{"artifacts": ["x"], "commit": "abc"}',
    )
    assert fp1 == fp2
    with pytest.raises(ValueError, match="valid JSON"):
        ka.resolve_subject(
            conn, board=_board_slug(), subject_kind="release_plan",
            subject_ref="not json",
        )


def test_document_fingerprint_tracks_content_not_path(kanban_home, tmp_path):
    conn = _connect()
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("same content")
    b.write_text("same content")
    _, fp_a = ka.resolve_subject(
        conn, board=_board_slug(), subject_kind="document", subject_ref=str(a))
    _, fp_b = ka.resolve_subject(
        conn, board=_board_slug(), subject_kind="document", subject_ref=str(b))
    assert fp_a == fp_b
    a.write_text("changed content")
    _, fp_a2 = ka.resolve_subject(
        conn, board=_board_slug(), subject_kind="document", subject_ref=str(a))
    assert fp_a2 != fp_a
    with pytest.raises(ValueError, match="no stable subject"):
        ka.resolve_subject(
            conn, board=_board_slug(), subject_kind="document",
            subject_ref=str(tmp_path / "missing.md"),
        )


def test_no_approval_without_stable_subject(kanban_home):
    conn = _connect()
    with pytest.raises(ValueError, match="does not exist"):
        _request(conn, subject_ref="t_missing")
    with pytest.raises(ValueError, match="subject_kind"):
        _request(conn, subject_kind="bogus")
    with pytest.raises(ValueError, match="type must be"):
        _request(conn, type="bogus")
    assert int(conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == 0
    assert int(conn.execute("SELECT COUNT(*) FROM approval_events").fetchone()[0]) == 0
    conn.close()


# ---------------------------------------------------------------- budget subjects


def test_budget_approval_needs_stable_budget_row(kanban_home):
    conn = _connect()
    # The period under test is derived from the clock, not hardcoded: a
    # literal month goes stale at the month boundary (set_budget defaults to
    # the current month, so a fixed period stops matching it on the 1st).
    period = kc.current_period()
    # No period -> refused.
    with pytest.raises(ValueError, match="--period"):
        _request(conn, type="budget", subject_kind="budget",
                 subject_ref="profile:alice")
    # No budget row -> refused (no approval without a stable subject).
    with pytest.raises(ValueError, match="no stable subject"):
        _request(conn, type="budget", subject_kind="budget",
                 subject_ref="profile:alice", period=period)
    kc.set_budget(conn, board=_board_slug(), scope="profile", ref="alice",
                  limit_usd=5.0, period=period)
    result = _request(conn, type="budget", subject_kind="budget",
                      subject_ref="profile:alice", period=period)
    assert result["period"] == period
    assert result["subject_id"] == "profile:alice"
    # Changing the limit drifts the subject -> decision invalidates.
    kc.set_budget(conn, board=_board_slug(), scope="profile", ref="alice", limit_usd=9.0)
    with pytest.raises(ka.ApprovalStateError, match="invalidated"):
        _decide(conn, result["id"], "approve")
    assert _status(conn, result["id"]) == "invalidated"
    conn.close()


# ---------------------------------------------------------------- release on approve


def _block_bound(conn, tid, ap_id, *, kind="needs_input"):
    assert kb.block_task(conn, tid, reason=f"approval:{ap_id}", kind=kind)


def test_approve_releases_exactly_bound_tasks(kanban_home):
    conn = _connect()
    bound = kb.create_task(conn, title="bound", assignee="a")
    other_approval = kb.create_task(conn, title="other approval", assignee="a")
    plain_blocked = kb.create_task(conn, title="plain", assignee="a")
    ready = kb.create_task(conn, title="ready", assignee="a")
    ap = _request(conn)["id"]
    ap2 = _request(conn, requester="bob")["id"]

    _block_bound(conn, bound, ap)
    _block_bound(conn, other_approval, ap2)
    assert kb.block_task(conn, plain_blocked, reason="waiting for peter", kind="needs_input")
    for tid in (bound, other_approval, plain_blocked):
        assert kb.get_task(conn, tid).status == "blocked"

    decided = _decide(conn, ap, "approve")
    # EXACTLY the task bound by approval:<ap> is released — the others keep
    # their blocks (Vorher/Nachher-Statusdiff).
    assert decided["released_tasks"] == [bound]
    assert kb.get_task(conn, bound).status == "ready"
    assert kb.get_task(conn, other_approval).status == "blocked"
    assert kb.get_task(conn, plain_blocked).status == "blocked"
    assert kb.get_task(conn, ready).status == "ready"
    # The release used the shared unblock semantics: unblocked event present.
    assert int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'unblocked'",
        (bound,),
    ).fetchone()[0]) == 1

    # reject/revise never release anything. The re-bind uses an UNTYPED block:
    # a second needs_input block of the same card would route to triage
    # (BLOCK_RECURRENCE_LIMIT) — different kernel semantics, not this test.
    _block_bound(conn, bound, ap2, kind=None)
    decided = _decide(conn, ap2, "reject")
    assert decided.get("released_tasks", []) == []
    assert kb.get_task(conn, bound).status == "blocked"
    conn.close()


def test_rebind_after_unblock_only_latest_reason_counts(kanban_home):
    conn = _connect()
    tid = kb.create_task(conn, title="victim", assignee="a")
    ap = _request(conn)["id"]
    ap_other = _request(conn, requester="bob")["id"]
    # Untyped first block: the re-bind below must land in 'blocked', not in
    # triage (a same-kind re-block would hit BLOCK_RECURRENCE_LIMIT).
    _block_bound(conn, tid, ap_other, kind=None)
    kb.unblock_task(conn, tid)
    _block_bound(conn, tid, ap)
    # Deciding ap_other (whose reason is no longer the latest) releases
    # NOTHING: only the latest blocked event's exact reason counts.
    decided = _decide(conn, ap_other, "approve")
    assert decided["released_tasks"] == []
    assert kb.get_task(conn, tid).status == "blocked"
    # The task bound by the CURRENT latest reason is released by its approval.
    decided = _decide(conn, ap, "approve")
    assert decided["released_tasks"] == [tid]
    conn.close()


# ---------------------------------------------------------------- board/tenant


def test_board_isolation(kanban_home):
    conn = _connect()
    result = _request(conn)
    ap = result["id"]

    # A second board is a separate DB with its own board slug: the approval
    # is invisible there and cannot be decided through it.
    other = kbc.connect(board="b2")
    try:
        assert ka.list_approvals(other, "b2") == []
        with pytest.raises(LookupError):
            ka.get_approval(other, ap)
        with pytest.raises(LookupError):
            ka.decide_approval(other, ap, decision="approve", approver="peter")
    finally:
        other.close()
    assert _status(conn, ap) == "pending"
    conn.close()


def test_task_subject_stamps_tenant(kanban_home):
    conn = _connect()
    tid = kb.create_task(conn, title="tenant subject", assignee="a", tenant="acme")
    result = _request(conn, subject_ref=tid)
    assert result["tenant"] == "acme"
    conn.close()


# ---------------------------------------------------------------- show hint


def test_show_hint_for_bound_task(approvals_on):
    from tools import kanban_tools as kt

    conn = _connect()
    tid = kb.create_task(conn, title="hinted", assignee="a")
    ap = _request(conn, subject_ref=tid)["id"]
    conn.close()

    payload = json.loads(kt._handle_show({"task_id": tid}))
    assert "approval" not in payload  # not blocked yet -> no hint

    conn = _connect()
    _block_bound(conn, tid, ap)
    conn.close()
    payload = json.loads(kt._handle_show({"task_id": tid}))
    assert payload["approval"] == {"id": ap, "status": "pending", "type": "action"}

    conn = _connect()
    _decide(conn, ap, "approve")
    conn.close()
    # The approve RELEASED the task (it is no longer blocked) — the hint is a
    # projection of the block, so it disappears with it.
    payload = json.loads(kt._handle_show({"task_id": tid}))
    assert "approval" not in payload
    # ... but the approval itself reads back as approved.
    conn = _connect()
    assert ka.get_approval(conn, ap)["status"] == "approved"
    conn.close()


# ---------------------------------------------------------------- CLI surface


def test_cli_approval_roundtrip(kanban_home, capsys, monkeypatch):
    import argparse

    from hermes_cli.kanban import _cmd_approval
    from hermes_cli.kanban_parser import build_parser

    # Deterministic identities for the CLI path (profile resolution is
    # environment-dependent; the contract under test is the flow, not the name).
    # alice requests, peter decides — anything else would be self-approval.
    monkeypatch.setattr(ka, "_acting_profile", lambda: "alice")

    wrap = argparse.ArgumentParser(prog="kanban-wrap")
    parser = build_parser(wrap.add_subparsers(dest="_top"))

    def run(argv):
        args = parser.parse_args(argv)
        assert args.kanban_action == "approval"
        return _cmd_approval(args), capsys.readouterr().out

    def as_profile(name, argv):
        monkeypatch.setattr(ka, "_acting_profile", lambda: name)
        try:
            return run(argv)
        finally:
            monkeypatch.setattr(ka, "_acting_profile", lambda: "alice")

    conn = _connect()
    tid = kb.create_task(conn, title="cli subject", assignee="a")
    conn.close()

    rc, out = run(["approval", "request", "action",
                   "--subject-kind", "task", "--subject-ref", tid,
                   "--note", "needs a human"])
    assert rc == 0 and "approval request" in out
    rc, out = run(["approval", "list", "--json"])
    assert rc == 0
    ap = json.loads(out)[0]["id"]
    conn = _connect()
    assert _status(conn, ap) == "pending"
    conn.close()

    rc, out = run(["approval", "show", "--json", ap])
    assert rc == 0
    shown = json.loads(out)
    assert shown["approval"]["id"] == ap
    assert [e["kind"] for e in shown["events"]] == ["approval_requested"]

    # Duplicate request dedupes to the same id via the CLI too.
    rc, out = run(["approval", "request", "action",
                   "--subject-kind", "task", "--subject-ref", tid])
    assert rc == 0 and "deduplicated" in out

    # Reject with a reason; the audit trail carries it.
    rc, out = as_profile("peter", ["approval", "reject", ap, "--note", "not this week"])
    assert rc == 0 and "rejected" in out
    conn = _connect()
    events = ka.approval_events(conn, ap)
    assert events[-1]["kind"] == "approval_decided"
    assert events[-1]["payload"]["decision"] == "reject"
    assert events[-1]["payload"]["note"] == "not this week"
    conn.close()

    # Deciding again fails with exit != 0 and a clear message.
    rc, out = as_profile("peter", ["approval", "approve", ap])
    assert rc == 1 and "already decided" in out

    # Drifted subject: approve fails (exit != 0), status invalidated, exactly
    # one invalidation event (binary acceptance criterion 1).
    conn = _connect()
    tid2 = kb.create_task(conn, title="drift subject", body="v1", assignee="a")
    conn.close()
    rc, out = run(["approval", "request", "action", "--subject-kind", "task",
                   "--subject-ref", tid2, "--json"])
    assert rc == 0
    ap2 = json.loads(out)["id"]
    conn = _connect()
    conn.execute("UPDATE tasks SET body = 'v2' WHERE id = ?", (tid2,))
    conn.close()
    rc, out = as_profile("peter", ["approval", "approve", ap2])
    assert rc == 1 and "invalidated" in out
    conn = _connect()
    assert _status(conn, ap2) == "invalidated"
    assert _events(conn, ap2, "approval_invalidated") == 1
    conn.close()

    # Self-approval through the CLI: alice approving her own request fails
    # with exit != 0 (rc 2) and no state change.
    conn = _connect()
    tid3 = kb.create_task(conn, title="self subject", assignee="a")
    conn.close()
    rc, out = run(["approval", "request", "action", "--subject-kind", "task",
                   "--subject-ref", tid3, "--json"])
    ap3 = json.loads(out)["id"]
    rc, out = as_profile("alice", ["approval", "approve", ap3])
    assert rc == 2 and "self-approval" in out
    conn = _connect()
    assert _status(conn, ap3) == "pending"
    conn.close()
