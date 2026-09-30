"""Kanban project governance stage 3: goals, owners, budgets, read-only rollups.

Spec: docs/kanban-governance-spec.md §5 (Stufe 3). These tests prove the
stage's contract over the EXISTING ``tasks.project_id`` (no goal engine, the
per-task goal_mode loop stays untouched):

* reference validation is fail-closed: a project unknown to BOTH the projects
  registry and the board is rejected on write AND read; a registry project
  bound to another board (foreign board reference) is rejected; an explicit
  tenant without board-side footprint is rejected (tenant stays a soft
  namespace — projects without footprint accept any tenant binding);
* slug references canonicalise to the registry id (one row per project);
* a project known to the board only (cross-profile: the creator's registry is
  invisible here) is accepted — the spec's "Board-first" path;
* goal upsert is idempotent with None=unchanged / ""=clear merge semantics;
* ``--budget`` couples to EXACTLY ONE ``kanban_budgets`` row (scope 'project',
  recurring 'persist'), idempotent on repeated calls, 0 removes it;
* the rollup is arithmetically exact: per-status counts match a direct SQL
  recount, MTD counts only estimated/actual ledger rows (unknown runs are
  reported as ``unknown_runs`` and NEVER add 0), other projects' tasks and
  costs do not leak, the tenant filter confines the projection, and the CLI
  JSON equals the kernel result;
* the rollup's statement count is bounded (no N+1, no unbounded scan): the
  same constant number of SQL statements for 5 and for 50 tasks;
* legacy DBs migrate additively (table + index recreated on open) and
  project-less tasks stay valid;
* workers can never write: the kernel refuses dispatched-worker contexts, the
  toolset registers the read-only ``kanban_project_rollup`` and no goal-set
  tool, and rollup reads stay open to workers;
* board isolation: each board governs its own goal row for the same project.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_cost as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_projects as kp
from hermes_cli import projects_db as pdb


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
def repo_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _board_slug() -> str:
    return kb.get_current_board() or kb.DEFAULT_BOARD


def _connect():
    return kbc.connect()


def _registry_project(name: str, *, board_slug=None, repo=None, primary_path=None) -> str:
    with pdb.connect_closing() as pconn:
        return pdb.create_project(
            pconn, name=name, primary_path=str(primary_path), board_slug=board_slug,
        )


def _project_task(conn, project_id: str, *, tenant=None, title="t") -> str:
    tid = kb.create_task(conn, title=title, assignee="alice", project_id=project_id, tenant=tenant)
    assert kb.get_task(conn, tid).project_id == project_id
    return tid


def _set_status(conn, tid: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))


def _ledger_row(conn, run_id: int, task_id: str, project_id: str, *, period,
                cost_status, estimated=None, actual=None) -> None:
    conn.execute(
        "INSERT INTO task_run_costs (run_id, task_id, board, tenant, project_id, profile, "
        "estimated_cost_usd, actual_cost_usd, cost_status, cost_source, period, recorded_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, task_id, _board_slug(), None, project_id, "alice",
         estimated, actual, cost_status, f"test:{cost_status}", period, 1, 1),
    )


def _period_now() -> str:
    return time.strftime("%Y-%m", time.gmtime())


# ---------------------------------------------------------------- migration


def test_legacy_db_creates_project_tables_and_serves(kanban_home):
    conn = _connect()
    legacy_tid = kb.create_task(conn, title="history", assignee="alice")
    conn.close()

    # Simulate a pre-governance board: stage-3 table and index absent.
    path = kb.kanban_db_path()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE IF EXISTS kanban_project_goals")
    raw.execute("DROP INDEX IF EXISTS idx_tasks_project")
    raw.commit()
    raw.close()
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))

    conn = _connect()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "kanban_project_goals" in tables
        assert "idx_tasks_project" in indexes

        # Old cards keep working through the untouched kernel paths.
        assert kb.get_task(conn, legacy_tid).status == "ready"
        kb.add_comment(conn, legacy_tid, "alice", "still fine")
        fresh = kb.create_task(conn, title="new card", assignee="alice")
        assert kb.get_task(conn, fresh).project_id is None
    finally:
        conn.close()


def test_tasks_without_project_stay_valid_and_ungoverned(kanban_home):
    conn = _connect()
    try:
        plain = kb.create_task(conn, title="no project", assignee="alice")
        assert kb.get_task(conn, plain).project_id is None

        pid = _registry_project("Widget", primary_path=kanban_home / "r1")
        row = kp.set_project_goal(conn, board=_board_slug(), project_id=pid, goal="g")
        assert row["project_id"] == pid

        # The project rollup counts ONLY the project's tasks — the plain task
        # does not leak into it.
        rollup = kp.project_rollup(conn, board=_board_slug(), project_id=pid)
        assert rollup["tasks"]["total"] == 0
        assert kb.get_task(conn, plain).project_id is None
    finally:
        conn.close()


# ------------------------------------------------- reference validation


def test_unknown_project_reference_rejected_on_write_and_read(kanban_home):
    conn = _connect()
    try:
        with pytest.raises(kp.ProjectReferenceError, match="unknown project reference"):
            kp.set_project_goal(conn, board=_board_slug(), project_id="p_nope", goal="x")
        with pytest.raises(kp.ProjectReferenceError, match="unknown project reference"):
            kp.project_rollup(conn, board=_board_slug(), project_id="nope-slug")
        assert conn.execute("SELECT COUNT(*) FROM kanban_project_goals").fetchone()[0] == 0
    finally:
        conn.close()


def test_foreign_board_reference_rejected(kanban_home):
    conn = _connect()
    try:
        foreign = _registry_project(
            "Elsewhere", board_slug="other-board", primary_path=kanban_home / "r2")
        with pytest.raises(kp.ProjectReferenceError, match="foreign board reference"):
            kp.set_project_goal(conn, board=_board_slug(), project_id=foreign, goal="x")
        with pytest.raises(kp.ProjectReferenceError, match="foreign board reference"):
            kp.project_rollup(conn, board=_board_slug(), project_id=foreign)
        # An unbound registry project (board_slug NULL) is fine on this board.
        local = _registry_project("Local", primary_path=kanban_home / "r3")
        kp.set_project_goal(conn, board=_board_slug(), project_id=local, goal="ok")
        assert kp.get_project_goal(conn, _board_slug(), local)["goal"] == "ok"
    finally:
        conn.close()


def test_foreign_tenant_reference_rejected(kanban_home):
    conn = _connect()
    try:
        pid = _registry_project("Tenanted", primary_path=kanban_home / "r4")
        _project_task(conn, pid, tenant="alpha")

        with pytest.raises(kp.ProjectReferenceError, match="foreign tenant reference"):
            kp.set_project_goal(conn, board=_board_slug(), project_id=pid, tenant="gamma")

        row = kp.set_project_goal(conn, board=_board_slug(), project_id=pid, tenant="alpha")
        assert row["tenant"] == "alpha"

        # Soft namespace: a project without tenant footprint accepts any
        # binding; "" clears it.
        virgin = _registry_project("Virgin", primary_path=kanban_home / "r5")
        row = kp.set_project_goal(conn, board=_board_slug(), project_id=virgin, tenant="any")
        assert row["tenant"] == "any"
        row = kp.set_project_goal(conn, board=_board_slug(), project_id=virgin, tenant="")
        assert row["tenant"] is None
    finally:
        conn.close()


def test_slug_reference_canonicalises_to_registry_id(kanban_home):
    conn = _connect()
    try:
        pid = _registry_project("Widget", primary_path=kanban_home / "r6")
        by_slug = kp.set_project_goal(conn, board=_board_slug(), project_id="widget", goal="g1")
        by_id = kp.set_project_goal(conn, board=_board_slug(), project_id=pid, owner="peter")
        assert by_slug["project_id"] == pid == by_id["project_id"]
        assert conn.execute("SELECT COUNT(*) FROM kanban_project_goals").fetchone()[0] == 1
    finally:
        conn.close()


def test_board_side_only_project_accepted(kanban_home):
    """Spec §5(d) Board-first: the creator's profile-local registry is not
    visible here, but the shared board knows the project through its tasks."""
    conn = _connect()
    try:
        # A task written by another profile (registry-less here): direct row.
        other = "p_cross_profile"
        now = int(time.time())
        with kbc.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, workspace_kind, project_id, tenant) "
                "VALUES ('t_cross', 'from another profile', 'ready', ?, 'dir', ?, 'shared')",
                (now, other),
            )

        row = kp.set_project_goal(conn, board=_board_slug(), project_id=other, goal="cross")
        assert row["project_id"] == other
        rollup = kp.project_rollup(conn, board=_board_slug(), project_id=other)
        assert rollup["tasks"]["total"] == 1
        assert rollup["tenants"] == ["shared"]
    finally:
        conn.close()


# ------------------------------------------------- goal row semantics


def test_goal_upsert_merge_semantics_and_idempotence(kanban_home):
    conn = _connect()
    try:
        pid = _registry_project("Upsert", primary_path=kanban_home / "r7")
        first = kp.set_project_goal(
            conn, board=_board_slug(), project_id=pid, goal="ship it", owner="peter",
            status="active", created_by="alice", now=1000,
        )
        assert (first["goal"], first["owner"], first["status"]) == ("ship it", "peter", "active")

        # None = unchanged, "" = clear; repeated calls stay one row.
        second = kp.set_project_goal(
            conn, board=_board_slug(), project_id=pid, owner="", now=2000,
        )
        assert second["owner"] is None
        assert second["goal"] == "ship it"
        assert second["status"] == "active"  # unchanged
        assert second["created_by"] == "alice"  # preserved on update
        assert second["created_at"] == 1000  # preserved on update
        assert second["updated_at"] == 2000

        third = kp.set_project_goal(
            conn, board=_board_slug(), project_id=pid, goal="", status="achieved", now=3000,
        )
        assert third["goal"] is None
        assert third["status"] == "achieved"

        assert conn.execute("SELECT COUNT(*) FROM kanban_project_goals").fetchone()[0] == 1

        with pytest.raises(ValueError, match="status must be one of"):
            kp.set_project_goal(conn, board=_board_slug(), project_id=pid, status="bogus")
    finally:
        conn.close()


def test_budget_coupling_writes_exactly_one_row(kanban_home):
    conn = _connect()
    try:
        pid = _registry_project("Budgeted", primary_path=kanban_home / "r8")

        kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=10)
        kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=10)
        rows = conn.execute(
            "SELECT period, limit_usd FROM kanban_budgets WHERE board = ? AND scope = 'project' AND ref = ?",
            (_board_slug(), pid),
        ).fetchall()
        assert len(rows) == 1  # idempotent on the double call
        assert rows[0]["period"] == kc.PERSIST_PERIOD  # recurring monthly budget

        row = kp.get_project_goal(conn, _board_slug(), pid)
        assert row["monthly_budget_usd"] == 10.0  # goal row mirrors the budget

        # Update the value: still exactly one row.
        kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=25)
        rows = conn.execute(
            "SELECT limit_usd FROM kanban_budgets WHERE board = ? AND scope = 'project' AND ref = ?",
            (_board_slug(), pid),
        ).fetchall()
        assert [r["limit_usd"] for r in rows] == [25.0]

        # --budget 0 removes the coupled row and the mirrored amount.
        row = kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=0)
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_budgets WHERE board = ? AND scope = 'project' AND ref = ?",
            (_board_slug(), pid),
        ).fetchone()[0] == 0
        assert row["monthly_budget_usd"] is None

        with pytest.raises(ValueError, match="budget must be"):
            kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=-5)
    finally:
        conn.close()


# ------------------------------------------------- rollup arithmetic


def _seed_project_with_workload(conn, home, *, n_status_tasks=4):
    pid = _registry_project("Rollup", primary_path=home / "r9")
    tids = {
        "ready": _project_task(conn, pid, tenant="alpha", title="ready"),
        "done": _project_task(conn, pid, tenant="alpha", title="done"),
        "blocked": _project_task(conn, pid, tenant="beta", title="blocked"),
        "review": _project_task(conn, pid, tenant="beta", title="review"),
    }
    for status, tid in tids.items():
        _set_status(conn, tid, status)
    period = _period_now()

    # Ledger: one estimated, one actual, one unknown (never 0), one in
    # ANOTHER period (must not count), one for ANOTHER project (must not leak).
    with kbc.write_txn(conn):
        _ledger_row(conn, 101, tids["ready"], pid, period=period, cost_status="estimated", estimated=1.5)
        _ledger_row(conn, 102, tids["done"], pid, period=period, cost_status="actual", actual=2.25)
        _ledger_row(conn, 103, tids["blocked"], pid, period=period, cost_status="unknown")
        _ledger_row(conn, 104, tids["review"], pid, period="2000-01", cost_status="estimated", estimated=99.0)
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, status, started_at) VALUES (?,?,?,?,?)",
            (101, tids["ready"], "alice", "done", int(time.time())),
        )
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, status, started_at) VALUES (?,?,?,?,?)",
            (102, tids["done"], "alice", "done", int(time.time())),
        )
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, status, started_at) VALUES (?,?,?,?,?)",
            (103, tids["blocked"], "alice", "done", int(time.time())),
        )
    other = _registry_project("Other", primary_path=home / "r10")
    other_tid = _project_task(conn, other, tenant="alpha", title="other")
    with kbc.write_txn(conn):
        _ledger_row(conn, 105, other_tid, other, period=period, cost_status="estimated", estimated=50.0)
    return pid, tids, period


def test_rollup_counts_match_direct_sql_recount(kanban_home):
    conn = _connect()
    try:
        pid, tids, period = _seed_project_with_workload(conn, kanban_home)
        rollup = kp.project_rollup(conn, board=_board_slug(), project_id=pid)

        # Per-status counts: relationship test against a direct SQL recount.
        recount = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status",
                (pid,),
            ).fetchall()
        }
        assert rollup["tasks"]["by_status"] == recount
        assert rollup["tasks"]["total"] == sum(recount.values())
        assert rollup["tasks"]["blocked"] == recount.get("blocked", 0)
        assert rollup["tasks"]["open"] == sum(
            n for s, n in recount.items() if s not in ("done", "archived"))

        # Progress arithmetic is exact.
        done, open_count = recount.get("done", 0), rollup["tasks"]["open"]
        assert rollup["progress"]["done"] == done
        assert rollup["progress"]["done_ratio"] == done / (done + open_count)

        # Costs: known MTD only (1.5 + 2.25), unknown runs surfaced, other
        # period and other project do not leak.
        assert rollup["costs"]["mtd_usd"] == pytest.approx(3.75)
        assert rollup["costs"]["unknown_runs"] == 1
        assert rollup["costs"]["runs_total"] == 3
        assert rollup["costs"]["period"] == period

        # Tenants of the project, nothing else.
        assert rollup["tenants"] == ["alpha", "beta"]
        assert rollup["goal"] is None  # no goal row yet — reads stay honest

        # Tenant filter confines the projection (soft filter, read-side).
        beta = kp.project_rollup(conn, board=_board_slug(), project_id=pid, tenant="beta")
        assert beta["tasks"]["by_status"] == {"blocked": 1, "review": 1}
        assert beta["tenants"] == ["alpha", "beta"]  # unfiltered project fact
    finally:
        conn.close()


def test_rollup_budget_utilisation(kanban_home):
    conn = _connect()
    try:
        pid, _, _ = _seed_project_with_workload(conn, kanban_home)
        kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=4.5)
        rollup = kp.project_rollup(conn, board=_board_slug(), project_id=pid)
        assert rollup["budget"]["limit_usd"] == 4.5
        assert rollup["budget"]["state"] == "warn"  # 3.75 >= 4.5 * 0.8 = 3.6

        kp.set_project_goal(conn, board=_board_slug(), project_id=pid, monthly_budget_usd=3)
        rollup = kp.project_rollup(conn, board=_board_slug(), project_id=pid)
        assert rollup["budget"]["state"] == "stopped"  # 3.75 >= 3

        assert kp.project_rollup(conn, board=_board_slug(), project_id=pid)["goal"]["owner"] is None
    finally:
        conn.close()


def test_cli_rollup_json_equals_direct_sql(kanban_home, capsys):
    """Acceptance: CLI rollup JSON and a direct SQL recomputation agree."""
    conn = _connect()
    try:
        pid, _, period = _seed_project_with_workload(conn, kanban_home)
        recount = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status",
                (pid,),
            ).fetchall()
        }
        sql_mtd = float(conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) "
            "FROM task_run_costs WHERE project_id = ? AND period = ? "
            "AND cost_status IN ('estimated', 'actual')",
            (pid, period),
        ).fetchone()[0])
        conn.close()

        args = argparse.Namespace(
            project_action="rollup", project_id=pid, tenant=None, period=None, json=True)
        assert kp.dispatch_project(args) == 0
        emitted = json.loads(capsys.readouterr().out)

        assert emitted["tasks"]["by_status"] == recount
        assert emitted["costs"]["mtd_usd"] == pytest.approx(sql_mtd)
        assert emitted["costs"]["period"] == period
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def test_rollup_statement_count_is_bounded(kanban_home):
    """No N+1 / no unbounded scan: the same constant number of SQL statements
    for a small and a ten-times-larger project (relationship, not snapshot)."""
    conn = _connect()
    try:
        pid = _registry_project("Bulk", primary_path=kanban_home / "r11")

        def seed(n: int, offset: int) -> None:
            with kbc.write_txn(conn):
                conn.executemany(
                    "INSERT INTO tasks (id, title, status, created_at, workspace_kind, project_id, tenant) "
                    "VALUES (?, ?, 'ready', 1, 'dir', ?, 'alpha')",
                    [(f"t_b{offset + i}", f"bulk{offset + i}", pid) for i in range(n)],
                )

        def counted_rollup() -> int:
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            try:
                kp.project_rollup(conn, board=_board_slug(), project_id=pid)
            finally:
                conn.set_trace_callback(None)
            return len(statements)

        seed(5, 0)
        small = counted_rollup()
        seed(45, 100)  # 50 tasks total
        large = counted_rollup()
        assert large == small  # constant, independent of task count
        assert small <= 12  # bounded: aggregates, never per-task queries
    finally:
        conn.close()


def test_board_isolation_of_goal_rows(kanban_home):
    conn = _connect()
    try:
        pid = _registry_project("Shared", primary_path=kanban_home / "r12")
        kp.set_project_goal(conn, board="default", project_id=pid, goal="on default")
        conn.close()

        second = kbc.connect(board="second")
        try:
            kp.set_project_goal(second, board="second", project_id=pid, goal="on second")
            assert kp.get_project_goal(second, "second", pid)["goal"] == "on second"
            assert kp.get_project_goal(second, "default", pid) is None  # other board's row invisible
            rows = kp.list_project_goals(second, "second")
            assert [r["project_id"] for r in rows] == [pid]
        finally:
            second.close()

        conn = _connect()
        assert kp.get_project_goal(conn, "default", pid)["goal"] == "on default"
        assert len(kp.list_project_goals(conn, "default")) == 1
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# ------------------------------------------------- worker fencing + tools


def test_worker_context_cannot_set_goals(kanban_home, monkeypatch):
    conn = _connect()
    try:
        pid = _registry_project("Fenced", primary_path=kanban_home / "r13")
        kp.set_project_goal(conn, board=_board_slug(), project_id=pid, goal="before")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dispatched")

        with pytest.raises(PermissionError, match="dispatched kanban worker"):
            kp.set_project_goal(conn, board=_board_slug(), project_id=pid, goal="nope")
        # The refused write changed nothing.
        assert kp.get_project_goal(conn, _board_slug(), pid)["goal"] == "before"
        # Reads stay open to workers.
        assert kp.project_rollup(conn, board=_board_slug(), project_id=pid)["tasks"]["total"] == 0
    finally:
        conn.close()


def test_toolset_has_rollup_read_only_and_no_goal_set_tool(kanban_home):
    import tools.kanban_tools  # noqa: F401  (registration happens at import)

    from tools.registry import registry

    assert registry.get_schema("kanban_project_rollup") is not None
    entry = registry.get_entry("kanban_project_rollup")
    assert entry.toolset == "kanban"
    # No worker surface writes goals/budgets: every registered project tool is
    # the read-only rollup.
    project_tools = [n for n in registry.get_all_tool_names() if n.startswith("kanban_project")]
    assert project_tools == ["kanban_project_rollup"]


def test_rollup_tool_handler_dispatch(kanban_home, monkeypatch):
    import tools.kanban_tools as kbt

    pid = _registry_project("Tooled", primary_path=kanban_home / "r14")
    conn = _connect()
    try:
        _project_task(conn, pid, tenant="alpha")
    finally:
        conn.close()

    result = json.loads(kbt._handle_project_rollup({"project_id": pid}))
    assert result["ok"] is True
    assert result["project_id"] == pid
    assert result["tasks"]["total"] == 1

    # Unknown reference fails closed through the tool surface too (ValueError
    # surfaces as a structured tool error).
    out = kbt._handle_project_rollup({"project_id": "p_nope2"})
    error = json.loads(out)
    assert "unknown project reference" in error["error"]


# ------------------------------------------------- CLI dispatch


def test_cli_goal_set_show_list(kanban_home, capsys):
    pid = _registry_project("Cli", primary_path=kanban_home / "r15")

    # Show without a row: error rc.
    args = argparse.Namespace(
        project_action="goal", project_id=pid, text=None, owner=None,
        budget=None, tenant=None, status=None, json=False)
    assert kp.dispatch_project(args) == 1
    assert "no goal row" in capsys.readouterr().out

    # Set with flags.
    args = argparse.Namespace(
        project_action="goal", project_id="cli", text="ship it", owner="peter",
        budget=12.5, tenant=None, status=None, json=True)
    assert kp.dispatch_project(args) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["project_id"] == pid  # slug canonicalised
    assert (row["goal"], row["owner"], row["monthly_budget_usd"]) == ("ship it", "peter", 12.5)

    # Show reads it back.
    args = argparse.Namespace(
        project_action="goal", project_id=pid, text=None, owner=None,
        budget=None, tenant=None, status=None, json=True)
    assert kp.dispatch_project(args) == 0
    assert json.loads(capsys.readouterr().out)["goal"] == "ship it"

    # List emits the board's rows.
    args = argparse.Namespace(project_action="list", json=True)
    assert kp.dispatch_project(args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["project_id"] for r in listed] == [pid]

    # Validation errors reach the CLI surface as rc 1.
    args = argparse.Namespace(
        project_action="goal", project_id="p_unknown", text="x", owner=None,
        budget=None, tenant=None, status=None, json=False)
    assert kp.dispatch_project(args) == 1
    assert "unknown project reference" in capsys.readouterr().out


def test_cli_goal_write_refused_for_worker(monkeypatch, capsys, tmp_path):
    """The CLI goal write fails closed in dispatched-worker contexts (rc 2)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    pid = _registry_project("Wfence", primary_path=tmp_path / "r16")

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dispatched")
    args = argparse.Namespace(
        project_action="goal", project_id=pid, text="nope", owner=None,
        budget=None, tenant=None, status=None, json=False)
    assert kp.dispatch_project(args) == 2
    assert "dispatched kanban worker" in capsys.readouterr().out
