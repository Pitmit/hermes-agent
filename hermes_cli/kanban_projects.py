"""Kanban project governance (governance stage 3): goals, owners, budgets, rollups.

Spec: ``docs/kanban-governance-spec.md`` §5 (Stufe 3). The EXISTING
``tasks.project_id`` (worktree anchor, ``kanban_db._resolve_project_link``) is
the shared key; this module adds the governance layer the spec calls for —
goal / owner / monthly-budget metadata plus a read-only rollup — and nothing
else. No decomposition, no judge, no goal engine: the per-task ``goal_mode``
loop and the ``/goal`` session semantics stay exactly as they are.

Three surfaces:

* **Kernel/API** — :func:`set_project_goal` (idempotent upsert on
  ``(board, project_id)``; ``None`` = unchanged, ``""`` = clear for text
  fields, ``--budget 0`` removes the budget row), :func:`get_project_goal`,
  :func:`list_project_goals` and :func:`project_rollup` (pure SQL read
  projection over ``tasks`` + ``task_run_costs`` + ``kanban_budgets`` + the
  goal row — no materialised cache, a bounded number of aggregate statements
  regardless of task count).
* **CLI** — :func:`dispatch_project` for ``hermes kanban project
  goal|rollup|list`` — the human surface.
* **Worker tool** — ``kanban_project_rollup`` (read-only). Workers may read
  rollups, never set goals/budgets: there is no goal-set tool and
  :func:`set_project_goal` refuses dispatched worker contexts
  (``HERMES_KANBAN_TASK``), exactly like approval decisions — no worker
  self-governance.

Reference validation (fail-closed, writes AND reads): a project reference
must be known to at least one of the two layers —

* the profile's projects registry (``projects.db``, read-only, looked up by
  id or slug and canonicalised to the registry id; a registry project bound
  to a DIFFERENT board via ``projects.board_slug`` is a foreign board
  reference → rejected), OR
* the board itself: any task on this board carrying the ``project_id``, or an
  existing goal row (the cross-profile case — the creating profile's
  projects.db is not visible here, but the shared board is; spec "Board-first").

Unknown to both layers → rejected. An explicit ``tenant`` the project has no
board-side task footprint in → rejected (fremde Tenant-Referenz); tenant
stays a soft namespace — projects without footprint yet, or multi-tenant
projects, are governed board-wide. Read-side tenant filters are NOT validated
(an empty filtered view is honest information, not a foreign reference).

Cost honesty (stage-1 contract): the rollup counts only ``estimated``/
``actual`` ledger rows toward MTD; unknown runs surface as ``unknown_runs`` —
never 0. Budget coupling goes through the ONE existing budget write path
(``kanban_cost.set_budget`` / ``remove_budget``, scope ``'project'``,
recurring period ``'persist'``) — a goal row with ``monthly_budget_usd`` and
its ``kanban_budgets`` row cannot diverge by construction.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Table lives in ``kanban_db.SCHEMA_SQL`` (single source of truth); this module
# only references it by name.
GOAL_TABLE = "kanban_project_goals"

VALID_GOAL_STATUSES = frozenset({"active", "achieved", "abandoned"})


class ProjectReferenceError(ValueError):
    """The project reference is not valid for this board (unknown/foreign)."""


# ---------------------------------------------------------------------------
# Context + board helpers
# ---------------------------------------------------------------------------

def _assert_human_governance_context() -> None:
    """Goal/budget writes are a human surface (no worker self-governance).

    ``HERMES_KANBAN_TASK`` is stamped by the dispatcher into every worker env;
    a dispatched worker — even one shelling out to ``hermes kanban project
    goal`` — is refused. Reads (rollup) stay open to workers everywhere.
    """
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        raise PermissionError(
            "project goals/budgets cannot be set from a dispatched kanban worker "
            "(HERMES_KANBAN_TASK is set). Run `hermes kanban project goal` from "
            "your own session.")


def _resolve_board_slug(board: Optional[str]) -> str:
    from hermes_cli import kanban_db as _kb

    return board or _kb.get_current_board() or _kb.DEFAULT_BOARD


def _registry_project(project_id: str):
    """Profile-local projects.db lookup (id or slug); ``None`` when absent.

    Fail-open on registry errors (board-first): an unreadable registry must not
    block governance of a project the board itself knows.
    """
    if not project_id:
        return None
    from hermes_cli import projects_db as _pdb

    try:
        with _pdb.connect_closing() as pconn:
            return _pdb.get_project(pconn, project_id)
    except Exception:
        return None


def _board_task_tenants(conn, project_id: str) -> set[str]:
    """Distinct non-empty task tenants of a project on this board."""
    return {
        row["tenant"]
        for row in conn.execute(
            "SELECT DISTINCT tenant FROM tasks "
            "WHERE project_id = ? AND tenant IS NOT NULL AND tenant != ''",
            (project_id,),
        ).fetchall()
    }


def _board_has_project(conn, project_id: str) -> bool:
    """Whether the board itself knows the project (task reference or goal row)."""
    if conn.execute(
        "SELECT 1 FROM tasks WHERE project_id = ? LIMIT 1", (project_id,)
    ).fetchone() is not None:
        return True
    return conn.execute(
        f"SELECT 1 FROM {GOAL_TABLE} WHERE project_id = ? LIMIT 1", (project_id,),
    ).fetchone() is not None


def validate_project_reference(
    conn, *, board: str, project_id: str, tenant: Optional[str] = None,
) -> str:
    """Canonical project id for a reference this board may govern — or raise.

    Fail-closed: unknown to both the projects registry and the board →
    ``ProjectReferenceError``; registry project bound to another board →
    ``ProjectReferenceError``; explicit tenant without board-side footprint →
    ``ProjectReferenceError`` (only when the project has a tenant footprint at
    all — tenant is a soft namespace, not an isolation wall).
    """
    board_slug = _resolve_board_slug(board)
    ref = str(project_id or "").strip()
    if not ref:
        raise ProjectReferenceError("project_id is required (registry id or slug)")

    obj = _registry_project(ref)
    if obj is not None:
        if obj.board_slug:
            from hermes_cli import kanban_db as _kb

            bound = obj.board_slug
            with contextlib.suppress(ValueError):
                bound = _kb._normalize_board_slug(obj.board_slug) or obj.board_slug
            if bound != board_slug:
                raise ProjectReferenceError(
                    f"foreign board reference: project '{obj.slug}' ({obj.id}) is bound "
                    f"to board '{obj.board_slug}', not this board ('{board_slug}')"
                )
        canonical = obj.id
    else:
        canonical = ref
        if not _board_has_project(conn, canonical):
            raise ProjectReferenceError(
                f"unknown project reference: {ref!r} is neither in the projects registry "
                f"nor known to this board (no task reference, no goal row). If you meant "
                f"a project slug, run from a profile whose registry knows it, or pass "
                f"the project id."
            )

    if tenant is not None:
        tenant_value = str(tenant).strip()
        if tenant_value:
            tenants = _board_task_tenants(conn, canonical)
            if tenants and tenant_value not in tenants:
                raise ProjectReferenceError(
                    f"foreign tenant reference: project {canonical} has board-side "
                    f"tasks only in tenant(s) {sorted(tenants)}, not {tenant_value!r}"
                )
    return canonical


# ---------------------------------------------------------------------------
# Goal row CRUD
# ---------------------------------------------------------------------------

_GOAL_COLUMNS = (
    "board, project_id, goal, owner, monthly_budget_usd, tenant, status, "
    "created_by, created_at, updated_at"
)


def _goal_row_dict(row) -> dict[str, Any]:
    return {
        "board": row["board"],
        "project_id": row["project_id"],
        "goal": row["goal"],
        "owner": row["owner"],
        "monthly_budget_usd": float(row["monthly_budget_usd"])
        if row["monthly_budget_usd"] is not None else None,
        "tenant": row["tenant"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_project_goal(conn, board: str, project_id: str) -> Optional[dict[str, Any]]:
    """The board's goal row for a project, or ``None`` (read-only, unvalidated)."""
    row = conn.execute(
        f"SELECT {_GOAL_COLUMNS} FROM {GOAL_TABLE} WHERE board = ? AND project_id = ?",
        (_resolve_board_slug(board), project_id),
    ).fetchone()
    return None if row is None else _goal_row_dict(row)


def list_project_goals(conn, board: str) -> list[dict[str, Any]]:
    """All goal rows of a board (read-only, unvalidated)."""
    return [
        _goal_row_dict(r)
        for r in conn.execute(
            f"SELECT {_GOAL_COLUMNS} FROM {GOAL_TABLE} WHERE board = ? ORDER BY project_id",
            (_resolve_board_slug(board),),
        ).fetchall()
    ]


def set_project_goal(
    conn, *, board: str, project_id: str, goal: Optional[str] = None,
    owner: Optional[str] = None, monthly_budget_usd: Optional[float] = None,
    tenant: Optional[str] = None, status: Optional[str] = None,
    created_by: str = "user", now: Optional[int] = None,
) -> dict[str, Any]:
    """Upsert the board's goal row for a project (human surface).

    Merge semantics: ``None`` = unchanged, ``""`` = clear for the text fields;
    ``monthly_budget_usd``: ``None`` = unchanged, ``> 0`` = set (plus the
    coupled ``kanban_budgets`` row via the one budget write path), ``0`` =
    remove the budget row. Idempotent: the same call twice leaves one row.
    """
    _assert_human_governance_context()
    if status is not None and status not in VALID_GOAL_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_GOAL_STATUSES)}, got {status!r}")

    board_slug = _resolve_board_slug(board)
    canonical = validate_project_reference(
        conn, board=board_slug, project_id=project_id, tenant=tenant,
    )

    new_budget: Optional[float]
    budget_action: Optional[str] = None
    if monthly_budget_usd is not None:
        try:
            new_budget = float(monthly_budget_usd)
        except (TypeError, ValueError):
            raise ValueError(f"budget must be a number, got {monthly_budget_usd!r}") from None
        if new_budget < 0:
            raise ValueError("budget must be >= 0 USD (0 removes the budget row)")
        budget_action = "set" if new_budget > 0 else "remove"
    else:
        new_budget = None

    now_val = int(now or time.time())
    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        row = conn.execute(
            f"SELECT {_GOAL_COLUMNS} FROM {GOAL_TABLE} WHERE board = ? AND project_id = ?",
            (board_slug, canonical),
        ).fetchone()

        def merged(field: str, value: Optional[str]) -> Optional[str]:
            """None = unchanged, "" = clear; absent row starts at NULL."""
            if value is None:
                return None if row is None else row[field]
            return str(value).strip() or None

        new_goal = merged("goal", goal)
        new_owner = merged("owner", owner)
        new_tenant = merged("tenant", tenant)
        new_status = status if status is not None else (
            "active" if row is None else row["status"]
        )
        stored_budget = new_budget if monthly_budget_usd is not None else (
            None if row is None else row["monthly_budget_usd"]
        )
        # A removed/cleared budget must not keep a stale amount in the row.
        if budget_action == "remove":
            stored_budget = None
        created_by_value = created_by if row is None else row["created_by"]
        created_at_value = now_val if row is None else row["created_at"]

        conn.execute(
            f"""INSERT INTO {GOAL_TABLE} ({_GOAL_COLUMNS})
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(board, project_id) DO UPDATE SET
                    goal = excluded.goal,
                    owner = excluded.owner,
                    monthly_budget_usd = excluded.monthly_budget_usd,
                    tenant = excluded.tenant,
                    status = excluded.status,
                    updated_at = excluded.updated_at""",
            (
                board_slug, canonical, new_goal, new_owner, stored_budget,
                new_tenant, new_status, created_by_value, created_at_value, now_val,
            ),
        )

    # Budget coupling AFTER the goal-row commit, through the ONE budget write
    # path (kanban_cost). Both sides are idempotent upserts on their primary
    # keys, so a crash between the two writes self-heals on the next call —
    # and the goal row (the human's contract) is never half-written.
    if budget_action in {"set", "remove"}:
        from hermes_cli import kanban_cost as _kc

        if budget_action == "set":
            _kc.set_budget(
                conn, board=board_slug, scope="project", ref=canonical,
                limit_usd=new_budget, monthly=True, created_by=created_by,
            )
        else:
            _kc.remove_budget(conn, board=board_slug, scope="project", ref=canonical, monthly=True)

    result = get_project_goal(conn, board_slug, canonical)
    assert result is not None  # the row was just upserted in this same process
    return result


# ---------------------------------------------------------------------------
# Rollup (read-only SQL projection)
# ---------------------------------------------------------------------------

def project_rollup(
    conn, *, board: str, project_id: str, tenant: Optional[str] = None,
    period: Optional[str] = None, now: Optional[int] = None,
) -> dict[str, Any]:
    """Read-only rollup of one project: statuses, progress, costs, budget, goal.

    Bounded statement count (no N+1, no unbounded scan): every number comes
    from one aggregate SQL statement over the ``idx_tasks_project`` index —
    the cost does not grow with the project's task count. Unknown costs stay
    ``unknown_runs`` (never 0 in the MTD sum). A tenant filter is a read-side
    filter, not a validation anchor (an empty filtered view is honest).
    """
    board_slug = _resolve_board_slug(board)
    canonical = validate_project_reference(conn, board=board_slug, project_id=project_id)

    from hermes_cli import kanban_cost as _kc

    period_value = str(period).strip() if period else _kc.current_period(now=now)

    tenant_value = str(tenant).strip() if tenant else None
    tenant_sql = " AND t.tenant = ?" if tenant_value else ""
    tenant_params = [tenant_value] if tenant_value else []

    # Task aggregation: one GROUP BY pass over the project index.
    rows = conn.execute(
        f"""SELECT t.status AS status, COUNT(*) AS n,
                   MAX(COALESCE(t.last_heartbeat_at, t.completed_at, t.started_at, t.created_at)) AS last
            FROM tasks t WHERE t.project_id = ?{tenant_sql}
            GROUP BY t.status""",
        [canonical, *tenant_params],
    ).fetchall()
    by_status = {r["status"]: int(r["n"]) for r in rows}
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    archived = by_status.get("archived", 0)
    open_count = total - done - archived
    blocked = by_status.get("blocked", 0)
    last_activity = max((r["last"] for r in rows if r["last"] is not None), default=None)

    # Known MTD spend of the project (estimated/actual only — unknown never 0).
    mtd = float(conn.execute(
        "SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) "
        "FROM task_run_costs "
        "WHERE board = ? AND project_id = ? AND period = ? "
        "AND cost_status IN ('estimated', 'actual')",
        (board_slug, canonical, period_value),
    ).fetchone()[0] or 0.0)

    # Runs of this project's tasks in the period, and how many of them have no
    # measured cost row (the honest upper bound of unaccounted work).
    runs = conn.execute(
        f"""SELECT COUNT(*),
                   SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM task_run_costs c
                        WHERE c.run_id = r.id AND c.cost_status != 'unknown')
                    THEN 1 ELSE 0 END)
            FROM task_runs r JOIN tasks t ON t.id = r.task_id
            WHERE t.project_id = ?{tenant_sql}
              AND strftime('%Y-%m', r.started_at, 'unixepoch') = ?""",
        [canonical, *tenant_params, period_value],
    ).fetchone()
    runs_total = int(runs[0] or 0)
    unknown_runs = int(runs[1] or 0) if runs[1] is not None else runs_total

    # Governing budget row: an exact-period row wins over the recurring one
    # (same precedence as the approval subject and the dispatcher gate).
    budget_row = conn.execute(
        "SELECT limit_usd, warn_ratio, period FROM kanban_budgets "
        "WHERE board = ? AND scope = 'project' AND ref = ? AND period IN (?, 'persist') "
        "ORDER BY CASE period WHEN ? THEN 0 ELSE 1 END LIMIT 1",
        (board_slug, canonical, period_value, period_value),
    ).fetchone()
    budget: Optional[dict[str, Any]] = None
    if budget_row is not None:
        limit = float(budget_row["limit_usd"] or 0.0)
        warn_ratio = float(budget_row["warn_ratio"] or 0.8)
        state = "stopped" if mtd >= limit else ("warn" if mtd >= limit * warn_ratio else "ok")
        budget = {
            "limit_usd": limit, "warn_ratio": warn_ratio,
            "period": budget_row["period"], "mtd_usd": mtd, "state": state,
        }

    return {
        "board": board_slug,
        "project_id": canonical,
        "goal": get_project_goal(conn, board_slug, canonical),
        "tasks": {
            "total": total,
            "open": open_count,
            "blocked": blocked,
            "by_status": by_status,
        },
        "progress": {
            "open": open_count,
            "done": done,
            "archived": archived,
            "done_ratio": (done / (done + open_count)) if (done + open_count) else None,
        },
        "costs": {
            "period": period_value,
            "runs_total": runs_total,
            "unknown_runs": unknown_runs,
            "mtd_usd": mtd,
        },
        "budget": budget,
        "tenants": sorted(_board_task_tenants(conn, canonical)),
        "last_activity": last_activity,
        "tenant_filter": tenant_value,
    }


# ---------------------------------------------------------------------------
# CLI surface: ``hermes kanban project goal|rollup|list``
# ---------------------------------------------------------------------------

def _cli_author() -> str:
    try:
        from hermes_cli.profiles import current_profile_name

        return current_profile_name("user") or "user"
    except Exception:
        return "user"


def _cli_err(message: str) -> int:
    print(f"kanban: {message}")
    return 1


def _dispatch_goal(args: argparse.Namespace, board: str) -> int:
    from hermes_cli import kanban_db_connect as _kbc

    mutation = any(
        getattr(args, name, None) is not None
        for name in ("text", "owner", "budget", "tenant", "status")
    )
    if not mutation:
        with _kbc.connect_closing() as conn:
            row = get_project_goal(conn, board, str(args.project_id).strip())
        if row is None:
            return _cli_err(
                f"no goal row for project {args.project_id!r} on board {board!r} "
                f"(set one: hermes kanban project goal {args.project_id} --text ...)"
            )
        if getattr(args, "json", False):
            print(json.dumps(row, indent=2, sort_keys=True))
            return 0
        print(f"Project {row['project_id']} (board {row['board']}) — status: {row['status']}")
        if row["goal"]:
            print(f"  goal:   {row['goal']}")
        if row["owner"]:
            print(f"  owner:  {row['owner']}")
        print(f"  budget: {row['monthly_budget_usd'] if row['monthly_budget_usd'] is not None else '—'} USD/month")
        if row["tenant"]:
            print(f"  tenant: {row['tenant']}")
        return 0

    try:
        with _kbc.connect_closing() as conn:
            row = set_project_goal(
                conn, board=board, project_id=args.project_id,
                goal=getattr(args, "text", None),
                owner=getattr(args, "owner", None),
                monthly_budget_usd=getattr(args, "budget", None),
                tenant=getattr(args, "tenant", None),
                status=getattr(args, "status", None),
                created_by=_cli_author(),
            )
    except PermissionError as exc:
        print(f"kanban: {exc}")
        return 2
    except ValueError as exc:
        return _cli_err(str(exc))
    if getattr(args, "json", False):
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0
    parts = [f"project goal set: {row['project_id']}"]
    if row["goal"]:
        parts.append(f"goal={row['goal']!r}")
    if row["owner"]:
        parts.append(f"owner={row['owner']}")
    if row["monthly_budget_usd"] is not None:
        parts.append(f"budget=${row['monthly_budget_usd']:.2f}/month")
    if row["tenant"]:
        parts.append(f"tenant={row['tenant']}")
    parts.append(f"status={row['status']}")
    print("  ".join(parts))
    return 0


def dispatch_project(args: argparse.Namespace) -> int:
    """``hermes kanban project goal|rollup|list`` — the human project surface.

    Workers never route through here for writes: the toolset exposes only the
    read-only ``kanban_project_rollup``, and the kernel refuses dispatched
    worker contexts on goal/budget writes (no self-governance).
    """
    from hermes_cli import kanban_db_connect as _kbc

    action = getattr(args, "project_action", None) or "list"
    board = _resolve_board_slug(None)

    if action == "goal":
        return _dispatch_goal(args, board)

    if action == "rollup":
        try:
            with _kbc.connect_closing() as conn:
                rollup = project_rollup(
                    conn, board=board, project_id=args.project_id,
                    tenant=getattr(args, "tenant", None) or None,
                    period=getattr(args, "period", None) or None,
                )
        except ValueError as exc:
            return _cli_err(str(exc))
        if getattr(args, "json", False):
            print(json.dumps(rollup, indent=2, sort_keys=True))
            return 0
        tasks = rollup["tasks"]
        progress = rollup["progress"]
        costs = rollup["costs"]
        print(f"Project {rollup['project_id']} on board {rollup['board']}")
        status_line = ", ".join(f"{k}={v}" for k, v in sorted(tasks["by_status"].items())) or "no tasks"
        print(f"  tasks: total={tasks['total']} open={tasks['open']} blocked={tasks['blocked']} ({status_line})")
        ratio = "unknown" if progress["done_ratio"] is None else f"{progress['done_ratio']:.0%}"
        print(f"  progress: done={progress['done']} open={progress['open']} done_ratio={ratio}")
        print(
            f"  costs ({costs['period']}): MTD known ${costs['mtd_usd']:.2f} "
            f"across {costs['runs_total']} run(s), {costs['unknown_runs']} unknown"
        )
        budget = rollup["budget"]
        if budget:
            print(
                f"  budget: ${budget['mtd_usd']:.2f} of ${budget['limit_usd']:.2f} "
                f"({budget['state']})"
            )
        else:
            print("  budget: none")
        goal = rollup["goal"]
        if goal and goal["goal"]:
            print(f"  goal: {goal['goal']}")
            if goal["status"] != "active":
                print(f"  goal status: {goal['status']}")
        return 0

    if action == "list":
        with _kbc.connect_closing() as conn:
            rows = list_project_goals(conn, board)
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print(f"No governed projects on board {board!r} (hermes kanban project goal <id> --text ...)")
            return 0
        for row in rows:
            budget = f"${row['monthly_budget_usd']:.2f}/mo" if row["monthly_budget_usd"] is not None else "—"
            tenant = row["tenant"] or "—"
            print(f"{row['project_id']:18s} status={row['status']:9s} budget={budget:12s} tenant={tenant}")
        return 0

    return _cli_err(f"unknown project action {action!r}")
