"""Kanban cost governance stage 1: run-cost ledger + monthly budget gate.

Spec: docs/kanban-governance-spec.md §3 (Stufe 1). These tests prove the
binary acceptance criteria of the stage:

* legacy board DB migrates (tables recreated, historical runs backfilled as
  honest 'unknown' rows with NULL amounts — never 0 — idempotently) and still
  opens for listing;
* a worker's exit flush attributes cost to EXACTLY its own run (run_id PK,
  fenced on the (run_id, task_id) pair, fill-only-from-unknown);
* the warn threshold lets the spawn through and emits exactly ONE deduplicated
  ``budget_warn`` event per (task, period);
* the hard stop prevents the spawn over two consecutive ticks with exactly ONE
  ``budget_stopped`` event, the card stays ``ready``; raising the limit
  reactivates the spawn only because the budget state changed (no new event);
* the month window is UTC 'YYYY-MM'; spend outside the window does not count;
* scope evaluation priority is board → tenant → project → profile (the first
  violated scope names the stop event);
* with ``kanban.budgets.enabled`` off (default) the gate never engages: no
  events, no ledger interaction, spawn unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_cost as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The test process may itself be a dispatched kanban worker — strip its
    # board/task env so board resolution is deterministic on the temp home.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def budgets_on(kanban_home):
    """``kanban.budgets.enabled: true`` through the real config loader."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  budgets:\n    enabled: true\n"
    )
    return kanban_home


# ---------------------------------------------------------------- helpers


def _board_slug() -> str:
    return kb.get_current_board() or kb.DEFAULT_BOARD


def _connect():
    return kbc.connect()


def _claim_run(conn, title, assignee, **create_kw):
    """Create + claim a task; returns (task_id, run_id).

    ``initial_status`` stays at its default ('running' intent): with no open
    parents ``initial_task_state`` lands the card in 'ready', which is what
    the claim needs.
    """
    tid = kb.create_task(conn, title=title, assignee=assignee, **create_kw)
    task = kb.claim_task(conn, tid, ttl_seconds=3600)
    assert task is not None, f"claim failed for {tid}"
    run_id = task.current_run_id or kb.get_task(conn, tid).current_run_id
    assert run_id, "claim did not stamp current_run_id"
    return tid, int(run_id)


def _seed_spend(conn, *, amount, status="estimated", assignee="alice", **create_kw):
    """One finished-looking run with known cost, attributed to `assignee`."""
    tid, run_id = _claim_run(conn, "seed-spend", assignee, **create_kw)
    usage = {
        "cost_status": status,
        "estimated_cost_usd": amount if status == "estimated" else None,
        "actual_cost_usd": amount if status == "actual" else None,
        "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10,
        "cache_write_tokens": 5, "reasoning_tokens": 0, "api_call_count": 2,
    }
    assert kc.record_worker_run_cost(conn, run_id, tid, board=_board_slug(), usage=usage)
    return tid, run_id


def _events(conn, task_id, kind) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()[0])


def _ready_task(conn, assignee="alice", **create_kw):
    """A card in 'ready' (default create intent + no parents lands ready)."""
    return kb.create_task(conn, title="victim", assignee=assignee, **create_kw)


# ---------------------------------------------------------------- migration


def test_legacy_db_backfills_unknown_rows_idempotently(kanban_home):
    conn = _connect()
    tid, run_id = _claim_run(conn, "history", "alice")
    conn.close()

    # Simulate a pre-governance board: both stage-1 tables absent.
    path = kb.kanban_db_path()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE IF EXISTS task_run_costs")
    raw.execute("DROP TABLE IF EXISTS kanban_budgets")
    raw.commit()
    raw.close()
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))

    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM task_run_costs WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert len(rows) == 1, "backfill must create exactly one row per historical run"
    row = rows[0]
    assert row["task_id"] == tid
    assert row["cost_status"] == "unknown"
    # Honest: unknown means NULL amounts — never 0.
    assert row["estimated_cost_usd"] is None
    assert row["actual_cost_usd"] is None
    # The migrated board still serves reads (criterion: `hermes kanban list`).
    assert any(t.id == tid for t in kb.list_tasks(conn))
    conn.close()

    # Idempotent: a fresh init cycle inserts nothing new.
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    conn = _connect()
    assert int(conn.execute(
        "SELECT COUNT(*) FROM task_run_costs WHERE run_id = ?", (run_id,)
    ).fetchone()[0]) == 1
    # unknown-never-0 invariant over the whole table.
    assert int(conn.execute(
        "SELECT COUNT(*) FROM task_run_costs WHERE cost_status = 'unknown' "
        "AND (estimated_cost_usd IS NOT NULL OR actual_cost_usd IS NOT NULL)"
    ).fetchone()[0]) == 0
    conn.close()


# ---------------------------------------------------------------- attribution


class _UsageDB:
    """SessionDB stand-in exposing exactly the flush's read surface."""

    def __init__(self, usage):
        self._usage = usage

    def session_spend_totals(self, session_id):
        assert session_id == "sess-1"
        return self._usage


def test_worker_flush_attributes_cost_to_its_run_exactly_once(kanban_home, monkeypatch):
    conn = _connect()
    tid, run_id = _claim_run(conn, "measured", "alice")
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", _board_slug())

    usage = {
        "cost_status": "estimated", "estimated_cost_usd": 1.25, "actual_cost_usd": None,
        "input_tokens": 120, "output_tokens": 60, "cache_read_tokens": 10,
        "cache_write_tokens": 5, "reasoning_tokens": 7, "api_call_count": 3,
    }
    assert kc.worker_run_cost_flush(_UsageDB(usage), "sess-1") is True

    conn = _connect()
    rows = conn.execute("SELECT * FROM task_run_costs WHERE run_id = ?", (run_id,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == tid
    assert row["profile"] == "alice"
    assert row["cost_status"] == "estimated"
    assert row["estimated_cost_usd"] == pytest.approx(1.25)
    assert row["input_tokens"] == 120 and row["api_call_count"] == 3
    assert row["period"] == kc.current_period()
    assert row["cost_source"] == "session_model_usage:alice"
    conn.close()

    # Fill-only-from-unknown: a second flush (e.g. SIGTERM epilogue after the
    # completion path) must NOT rewrite the measured row.
    cheaper = dict(usage, estimated_cost_usd=0.01)
    assert kc.worker_run_cost_flush(_UsageDB(cheaper), "sess-1") is True
    conn = _connect()
    row = conn.execute(
        "SELECT estimated_cost_usd, cost_status FROM task_run_costs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row["estimated_cost_usd"] == pytest.approx(1.25)
    assert row["cost_status"] == "estimated"
    conn.close()

    # The (run_id, task_id) fence: a sibling cannot attribute to a foreign run.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_not_a_real_task")
    assert kc.worker_run_cost_flush(_UsageDB(usage), "sess-1") is False


def test_worker_flush_without_usage_records_unknown_never_zero(kanban_home, monkeypatch):
    conn = _connect()
    tid, run_id = _claim_run(conn, "unmeasured", "alice")
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", _board_slug())

    assert kc.worker_run_cost_flush(_UsageDB(None), "sess-1") is True
    conn = _connect()
    row = conn.execute("SELECT * FROM task_run_costs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["cost_status"] == "unknown"
    assert row["estimated_cost_usd"] is None and row["actual_cost_usd"] is None
    assert row["input_tokens"] is None  # not measured -> NULL, not 0
    assert int(conn.execute(
        "SELECT COUNT(*) FROM task_run_costs WHERE cost_status = 'unknown' "
        "AND (estimated_cost_usd IS NOT NULL OR actual_cost_usd IS NOT NULL)"
    ).fetchone()[0]) == 0
    conn.close()


# ---------------------------------------------------------------- gate: warn


def test_warn_threshold_lets_spawn_through_and_emits_one_deduped_event(
    budgets_on, all_assignees_spawnable,
):
    conn = _connect()
    board = _board_slug()
    _seed_spend(conn, amount=8.5)  # MTD 8.50 of a 10 USD budget (warn at 80%)
    kc.set_budget(conn, board=board, scope="profile", ref="alice",
                  limit_usd=10.0, warn_ratio=0.8)
    tid = _ready_task(conn)

    spawned = []
    res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a[0].id) or 4242)
    assert res.spawned and res.spawned[0][0] == tid, "warn must NOT hold the spawn"
    assert _events(conn, tid, "budget_warn") == 1
    assert res.budget_stopped == []
    # Task was claimed -> running.
    assert kb.get_task(conn, tid).status == "running"

    # Dedup: the same unchanged budget state re-checked does not refire.
    decision = kc.budget_gate(conn, tid, assignee="alice", board=board, enabled=True)
    assert decision is not None and decision["kind"] == "budget_warn"
    assert _events(conn, tid, "budget_warn") == 1
    conn.close()


# ---------------------------------------------------------------- gate: stop


def test_hard_stop_holds_spawn_two_ticks_one_event_and_reactivates_on_raise(
    budgets_on, all_assignees_spawnable,
):
    conn = _connect()
    board = _board_slug()
    _seed_spend(conn, amount=11.0)  # MTD 11.00 >= limit 10
    kc.set_budget(conn, board=board, scope="profile", ref="alice",
                  limit_usd=10.0, warn_ratio=0.8)
    tid = _ready_task(conn)

    spawned = []
    stub = lambda *a, **k: spawned.append(a[0].id) or 4242
    res1 = kbd.dispatch_once(conn, spawn_fn=stub)
    assert not spawned, "hard stop must prevent the spawn"
    assert res1.budget_stopped == [(tid, "profile:alice")]
    assert kb.get_task(conn, tid).status == "ready", "stop is a gate, not a block"
    assert _events(conn, tid, "budget_stopped") == 1

    # Second tick: same state -> still held, exactly ONE event (idempotency).
    res2 = kbd.dispatch_once(conn, spawn_fn=stub)
    assert not spawned
    assert res2.budget_stopped == [(tid, "profile:alice")]
    assert _events(conn, tid, "budget_stopped") == 1
    assert kb.get_task(conn, tid).status == "ready"

    # Raising the limit changes the state -> next tick spawns, NO new event.
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=20.0)
    res3 = kbd.dispatch_once(conn, spawn_fn=stub)
    assert spawned == [tid], "raised limit must reactivate the spawn"
    assert _events(conn, tid, "budget_stopped") == 1
    assert kb.get_task(conn, tid).status == "running"
    conn.close()


# ---------------------------------------------------------------- month window


def test_mtd_window_is_utc_month_only(kanban_home, budgets_on):
    conn = _connect()
    board = _board_slug()
    now = int(time.time())
    prev = time.strftime("%Y-%m", time.gmtime(now - 32 * 86400))
    # Direct ledger row in the PREVIOUS month: must not count toward this month.
    with kbc.write_txn(conn):
        conn.execute(
            "INSERT INTO task_run_costs (run_id, task_id, board, profile, "
            "estimated_cost_usd, cost_status, period, recorded_at, updated_at) "
            "VALUES (999001, 't_old', ?, 'alice', 50.0, 'estimated', ?, ?, ?)",
            (board, prev, now, now),
        )
    assert kc.utc_period(0) == "1970-01"
    assert kc.current_period(now=now) == time.strftime("%Y-%m", time.gmtime(now))
    assert kc.mtd_spend(conn, board, "profile", "alice", kc.current_period(now=now)) == 0.0
    assert kc.mtd_spend(conn, board, "profile", "alice", prev) == pytest.approx(50.0)
    status = kc.budget_status(conn, board, kc.current_period(now=now))
    assert status["mtd_usd"] == 0.0
    conn.close()


# ---------------------------------------------------------------- scope priority


def test_scope_priority_most_general_violation_names_the_stop(budgets_on):
    conn = _connect()
    board = _board_slug()
    _seed_spend(conn, amount=8.5)  # board total AND profile alice both at 8.5

    # board limit 5 (violated) + profile limit 100 (fine) -> board names it.
    kc.set_budget(conn, board=board, scope="board", ref=None, limit_usd=5.0)
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=100.0)
    tid = _ready_task(conn)
    decision = kc.check_budget(conn, tid, assignee="alice", board=board)
    assert decision["stop"] is not None
    assert decision["stop"]["scope"] == "board"

    # Board lifted; profile alice over -> profile names it (bob unaffected).
    kc.set_budget(conn, board=board, scope="board", ref=None, limit_usd=100.0)
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=5.0)
    decision = kc.check_budget(conn, tid, assignee="alice", board=board)
    assert decision["stop"]["scope"] == "profile"
    assert decision["stop"]["ref"] == "alice"
    # A different profile's task is not gated by alice's budget.
    other = _ready_task(conn, assignee="bob")
    assert kc.check_budget(conn, other, assignee="bob", board=board)["stop"] is None
    conn.close()


def test_tenant_and_project_budgets_apply_to_matching_tasks_only(budgets_on):
    conn = _connect()
    board = _board_slug()
    # Real projects (create_task drops project_ids that do not resolve in
    # projects.db, so the seeded spend needs genuine project rows).
    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as pconn:
        proj_spent = pdb.create_project(pconn, name="cost-spent", board_slug=board)
        proj_idle = pdb.create_project(pconn, name="cost-idle", board_slug=board)

    # Known spend, attributed through the ledger's tenant/project columns
    # (inherited from each seeded task at claim time).
    _seed_spend(conn, amount=5.0, assignee="carol", tenant="contoso")
    _seed_spend(conn, amount=0.5, assignee="carol", project_id=proj_spent)

    tid_t = _ready_task(conn, tenant="acme")            # tenant acme: no spend
    tid_p = _ready_task(conn, project_id=proj_idle)     # project idle: no spend
    tid_other = _ready_task(conn, tenant="contoso")
    tid_p1 = _ready_task(conn, project_id=proj_spent)

    kc.set_budget(conn, board=board, scope="tenant", ref="acme", limit_usd=100.0)
    kc.set_budget(conn, board=board, scope="tenant", ref="contoso", limit_usd=3.0)
    kc.set_budget(conn, board=board, scope="project", ref=proj_spent, limit_usd=0.2)
    kc.set_budget(conn, board=board, scope="project", ref=proj_idle, limit_usd=100.0)

    # Matching budget + no spend -> no stop.
    assert kc.check_budget(conn, tid_t, assignee="alice", board=board)["stop"] is None
    assert kc.check_budget(conn, tid_p, assignee="alice", board=board)["stop"] is None
    # contoso task: its own tenant's budget is exhausted -> tenant names it.
    decision = kc.check_budget(conn, tid_other, assignee="alice", board=board)
    assert decision["stop"] is not None and decision["stop"]["scope"] == "tenant"
    assert decision["stop"]["ref"] == "contoso"
    # proj_spent task: its own project's budget is exhausted -> project names it.
    decision = kc.check_budget(conn, tid_p1, assignee="alice", board=board)
    assert decision["stop"] is not None and decision["stop"]["scope"] == "project"
    assert decision["stop"]["ref"] == proj_spent
    # A task with BOTH tenant and project exhausted: tenant (more general)
    # names the stop per the priority order board -> tenant -> project -> profile.
    tid_both = _ready_task(conn, tenant="contoso", project_id=proj_spent)
    decision = kc.check_budget(conn, tid_both, assignee="alice", board=board)
    assert decision["stop"]["scope"] == "tenant"
    # The acme task is NOT gated by contoso's or proj_spent's budgets.
    assert kc.check_budget(conn, tid_t, assignee="alice", board=board)["stop"] is None
    conn.close()


# ---------------------------------------------------------------- review mirror


def test_review_mirror_skips_budget_stopped_rows(budgets_on, all_assignees_spawnable):
    conn = _connect()
    board = _board_slug()
    _seed_spend(conn, amount=11.0)
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=10.0)
    tid = _ready_task(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))

    rows = kbd._lane_rows(conn, "review")
    assert rows, "review row must be enumerable"
    assert kbd._any_spawnable_review(conn, rows, board=board) is False, (
        "a budget-stopped review row must not withhold ready-lane capacity"
    )
    # Lifting the budget makes it spawnable again.
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=50.0)
    assert kbd._any_spawnable_review(conn, rows, board=board) is True
    conn.close()


# ---------------------------------------------------------------- flag off


def test_gate_off_leaves_dispatch_untouched(kanban_home, all_assignees_spawnable):
    conn = _connect()
    board = _board_slug()
    _seed_spend(conn, amount=11.0)  # would stop if the gate were armed
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=10.0)
    tid = _ready_task(conn)
    conn.close()

    # No config.yaml -> kanban.budgets.enabled default False.
    assert kc.budgets_enabled() is False
    conn = _connect()
    spawned = []
    res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a[0].id) or 4242)
    assert spawned == [tid], "flag off must leave the tick byte-identical"
    assert res.budget_stopped == []
    assert _events(conn, tid, "budget_stopped") == 0
    assert _events(conn, tid, "budget_warn") == 0
    conn.close()

    # And the gate short-circuits before touching the connection.
    class _Boom:
        def execute(self, *a, **k):
            raise AssertionError("disabled gate must not query")

    assert kc.budget_gate(_Boom(), "t_x", assignee="a", board="b", enabled=False) is None


# ---------------------------------------------------------------- config plumbing


def test_budgets_config_reads_through_the_real_loader(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Defaults: flag off, allow.
    assert kc.budgets_enabled() is False
    assert kc.unknown_policy() == "allow"

    (home / "config.yaml").write_text(
        "kanban:\n  budgets:\n    enabled: true\n    unknown_policy: flag\n"
    )
    assert kc.budgets_enabled() is True
    assert kc.unknown_policy() == "flag"

    # DEFAULT_CONFIG carries the keys with the same defaults (plumbing contract).
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    budgets = DEFAULT_CONFIG["kanban"]["budgets"]
    assert budgets["enabled"] is False
    assert budgets["unknown_policy"] == "allow"


def test_unknown_policy_flag_stamps_unknown_share_into_event(budgets_on):
    (budgets_on / "config.yaml").write_text(
        "kanban:\n  budgets:\n    enabled: true\n    unknown_policy: flag\n"
    )
    conn = _connect()
    board = _board_slug()
    _seed_spend(conn, amount=8.5)  # MTD 8.5 >= 10 * 0.8 -> warn crossing
    kc.set_budget(conn, board=board, scope="profile", ref="alice", limit_usd=10.0)
    tid = _ready_task(conn)
    decision = kc.budget_gate(conn, tid, assignee="alice", board=board, enabled=True)
    assert decision is not None and decision["kind"] == "budget_warn"
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (tid, "budget_warn"),
    ).fetchone()[0])
    # 'flag' policy stamps the unknown share into the event payload.
    assert "unknown_share" in payload
    assert "unknown_runs" in payload
    conn.close()


# ---------------------------------------------------------------- CLI surface


def test_cli_budget_set_show_rm_roundtrip(kanban_home, capsys):
    import argparse

    from hermes_cli.kanban import _cmd_budget
    from hermes_cli.kanban_parser import build_parser

    wrap = argparse.ArgumentParser(prog="kanban-wrap")
    parser = build_parser(wrap.add_subparsers(dest="_top"))

    def run(argv):
        args = parser.parse_args(argv)
        assert args.kanban_action == "budget"
        rc = _cmd_budget(args)
        return rc, capsys.readouterr().out

    rc, out = run(["budget", "set", "profile", "alice", "--limit", "5", "--warn", "0.9"])
    assert rc == 0 and "budget set" in out
    conn = _connect()
    rows = kc.list_budgets(conn, _board_slug())
    assert len(rows) == 1
    assert rows[0]["scope"] == "profile" and rows[0]["ref"] == "alice"
    assert rows[0]["limit_usd"] == pytest.approx(5.0)
    assert rows[0]["warn_ratio"] == pytest.approx(0.9)
    conn.close()

    # Upsert on the SAME key (period defaults to the current month) replaces.
    rc, out = run(["budget", "set", "profile", "alice", "--limit", "7"])
    assert rc == 0
    conn = _connect()
    rows = kc.list_budgets(conn, _board_slug())
    assert len(rows) == 1
    assert rows[0]["limit_usd"] == pytest.approx(7.0)
    conn.close()

    # --monthly is a DIFFERENT key ('persist' applies every month): two rows.
    rc, out = run(["budget", "set", "profile", "alice", "--limit", "9", "--monthly"])
    assert rc == 0
    conn = _connect()
    rows = kc.list_budgets(conn, _board_slug())
    assert len(rows) == 2
    assert {r["period"] for r in rows} == {"persist", kc.current_period()}
    conn.close()

    rc, out = run(["budget", "show", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert data["board"] == _board_slug()
    assert "mtd_usd" in data and "unknown_runs" in data and "unknown_share" in data
    assert data["gated_tasks"] == []
    assert any(b["scope"] == "profile" and b["period"] == "persist" for b in data["budgets"])

    # rm removes one key at a time; both keys must go before the table is empty.
    rc, out = run(["budget", "rm", "profile", "alice", "--monthly"])
    assert rc == 0
    conn = _connect()
    rows = kc.list_budgets(conn, _board_slug())
    assert len(rows) == 1 and rows[0]["period"] != "persist"
    conn.close()
    rc, out = run(["budget", "rm", "profile", "alice"])
    assert rc == 0
    conn = _connect()
    assert kc.list_budgets(conn, _board_slug()) == []
    conn.close()

    # Fail-closed validation.
    rc, out = run(["budget", "set", "profile", "bob", "--limit", "0"])
    assert rc == 1 and "limit must be > 0" in out
    rc, out = run(["budget", "set", "profile", "--limit", "3"])
    assert rc == 1 and "non-empty ref" in out
    rc, out = run(["budget", "rm", "profile", "alice"])
    assert rc == 1 and "no budget row" in out
    with pytest.raises(SystemExit):
        parser.parse_args(["budget", "set", "nonsense", "x", "--limit", "3"])


# ---------------------------------------------------------------- worker tool


def test_kanban_budget_show_tool_is_registered_and_read_only(kanban_home):
    from tools import kanban_tools as kt

    conn = _connect()
    kc.set_budget(conn, board=_board_slug(), scope="board", ref=None, limit_usd=4.0)
    conn.close()

    payload = json.loads(kt._handle_budget_show({"period": None}))
    assert payload["ok"] is True
    assert payload["board"] == _board_slug()
    assert any(
        b["scope"] == "board" and b["limit_usd"] == pytest.approx(4.0)
        for b in payload["budgets"]
    )
    # Registered on the kanban toolset (worker surface), schema declared.
    names = [row[0] for row in kt._TOOLS]
    assert "kanban_budget_show" in names
    assert kt.KANBAN_BUDGET_SHOW_SCHEMA["name"] == "kanban_budget_show"
    assert kt.KANBAN_BUDGET_SHOW_SCHEMA["parameters"]["required"] == []
