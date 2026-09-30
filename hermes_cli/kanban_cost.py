"""Kanban cost governance (governance stage 1): per-run cost ledger, monthly budgets, dispatcher budget gate.

Split out of the ``hermes_cli.kanban_*`` sibling family. Three surfaces share this module:

* **Kernel** — ``task_run_costs`` / ``kanban_budgets`` row helpers, the historical
  backfill (unknown rows, never 0) and the worker-side run-cost flush
  (:func:`worker_run_cost_flush`, called from the one-shot session-store flush so
  every worker exit path — completion, block, SIGTERM epilogue — records exactly
  one ledger row, fail-open).
* **Gate** — :func:`budget_gate` runs in the dispatcher tick *before* a claim when
  ``kanban.budgets.enabled`` is true. With the flag off (default) it returns
  ``None`` before issuing a single query, so an un-migrated tick is byte-identical.
* **CLI** — :func:`dispatch_budget` for ``hermes kanban budget set|show|rm``. The
  worker-facing ``kanban_budget_show`` tool is read-only on purpose: workers may
  see limits, never set them (no self-governance).

Honesty contract (spec): unknown costs are ``cost_status='unknown'`` with NULL
amounts — never 0. Budget sums count only ``estimated``/``actual`` rows; unknown
runs surface as ``unknown_runs``/``unknown_share`` in the budget report.

Scope evaluation priority: board → tenant → project → profile (most general
first). Every applicable budget is an independent cap; the first violated one in
that order is the one the ``budget_stopped`` event names. A warn-threshold
crossing emits a deduplicated ``budget_warn`` event but never holds the spawn.

Event idempotency: exactly one ``budget_stopped`` and one ``budget_warn`` event
per ``(task, period)`` — re-checked against ``task_events`` payloads before write,
so a tick does not refire while the budget state is unchanged. Raising a limit
above MTD lets the next tick spawn with no new event.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import time
from typing import Any
from typing import Optional

logger = logging.getLogger(__name__)

# Ledger/budget tables live in ``kanban_db.SCHEMA_SQL`` (single source of truth);
# this module only references them by name.
LEDGER_TABLE = "task_run_costs"
BUDGET_TABLE = "kanban_budgets"

VALID_SCOPES = frozenset({"board", "tenant", "project", "profile"})
# Ledger column that carries each scope's reference value.
_SCOPE_COLUMNS = {
    "profile": "profile",
    "project": "project_id",
    "tenant": "tenant",
}
# Gate evaluation priority: most general scope first; each row is an independent cap.
_SCOPE_PRIORITY = ("board", "tenant", "project", "profile")

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PERSIST_PERIOD = "persist"

STOP_EVENT_KIND = "budget_stopped"
WARN_EVENT_KIND = "budget_warn"


# ---------------------------------------------------------------------------
# Period + config helpers
# ---------------------------------------------------------------------------

def utc_period(epoch_seconds: Optional[int], *, now: Optional[int] = None) -> str:
    """``'YYYY-MM'`` (UTC) for a run-start timestamp — the run's budget period.

    A month rollover mid-run keeps the start period (deterministic, no update storm).
    A NULL/absent timestamp degrades to the current month, never an exception.
    """
    ts = epoch_seconds if epoch_seconds is not None else (now if now is not None else int(time.time()))
    return time.strftime("%Y-%m", time.gmtime(int(ts)))


def current_period(*, now: Optional[int] = None) -> str:
    """The current UTC month as ``'YYYY-MM'``."""
    return utc_period(None, now=now if now is not None else int(time.time()))


def _kanban_budgets_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly() or {}).get("kanban", {})
        return cfg.get("budgets") if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def budgets_enabled() -> bool:
    """``kanban.budgets.enabled`` (default False — the gate then never runs)."""
    try:
        return bool(_kanban_budgets_cfg().get("enabled", False))
    except Exception:
        return False


def unknown_policy() -> str:
    """``kanban.budgets.unknown_policy``: ``'allow'`` (default) or ``'flag'``.

    ``allow`` — unknown costs never count toward a hard stop (honest: not 0).
    ``flag`` — same accounting, plus ``unknown_share`` is stamped into budget
    events so boards with a high unknown share are visible in the event log.
    """
    try:
        policy = str(_kanban_budgets_cfg().get("unknown_policy", "allow")).strip().lower()
    except Exception:
        return "allow"
    return policy if policy in {"allow", "flag"} else "allow"


# ---------------------------------------------------------------------------
# Ledger writes
# ---------------------------------------------------------------------------

# Plain string (NOT a 1-tuple): interpolated directly into the INSERT header.
_LEDGER_COLUMNS = (
    "run_id, task_id, board, tenant, project_id, profile, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "reasoning_tokens, api_call_count, "
    "estimated_cost_usd, actual_cost_usd, cost_status, cost_source, "
    "period, recorded_at, updated_at"
)


def backfill_run_costs(conn, board: Optional[str] = None) -> int:
    """One-shot ledger backfill for historical runs: one ``unknown`` row each.

    Idempotent via ``INSERT OR IGNORE`` on the ``run_id`` primary key; NULL costs
    (never 0). Called once per board lifetime, right after the ledger table is
    first created (see ``kanban_db_connect._init_if_needed``).
    """
    from hermes_cli import kanban_db as _kb

    board_slug = board or _kb.get_current_board() or _kb.DEFAULT_BOARD
    now = int(time.time())
    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO {LEDGER_TABLE} (
                run_id, task_id, board, tenant, project_id, profile,
                cost_status, cost_source, period, recorded_at, updated_at)
            SELECT r.id, r.task_id, ?, t.tenant, t.project_id, r.profile,
                   'unknown', NULL,
                   COALESCE(strftime('%Y-%m', r.started_at, 'unixepoch'), 'unknown'),
                   ?, ?
            FROM task_runs r
            LEFT JOIN tasks t ON t.id = r.task_id
            """,
            (board_slug, now, now),
        )
        return int(cur.rowcount or 0)


def _resolve_board_slug(board: Optional[str]) -> str:
    from hermes_cli import kanban_db as _kb

    return board or _kb.get_current_board() or _kb.DEFAULT_BOARD


def record_worker_run_cost(
    conn,
    run_id: int,
    task_id: str,
    *,
    board: Optional[str],
    usage: Optional[dict[str, Any]],
    now: Optional[int] = None,
) -> bool:
    """Write the worker's OWN run cost row (exactly one; fill-only-from-unknown).

    Fence: the (run_id, task_id) pair must match a real ``task_runs`` row — the
    dispatcher stamps both into the worker env, so a sibling worker cannot
    attribute costs to an unrelated run any more than it could fake a heartbeat.
    ``usage`` comes from the worker's own ``session_model_usage`` rows
    (``SessionDB.session_spend_totals``); ``None`` means nothing was measured and
    the row stays ``cost_status='unknown'`` with NULL amounts.

    Append/fill-only: an existing *measured* row is never rewritten (a worker
    cannot lower its own costs later); only a backfilled ``unknown`` row is
    filled. Fail-open by contract — callers wrap in :func:`worker_run_cost_flush`.
    """
    run = conn.execute(
        "SELECT r.id, r.task_id, r.profile, r.started_at, t.tenant, t.project_id "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.id = ? AND r.task_id = ?",
        (int(run_id), task_id),
    ).fetchone()
    if run is None:
        return False

    now = int(now or time.time())
    period = utc_period(run["started_at"], now=now)
    board_slug = _resolve_board_slug(board)
    profile = run["profile"]

    status = "unknown"
    estimated = actual = None
    tokens: tuple = (None,) * 6
    cost_source = None
    if usage is not None:
        status = str(usage.get("cost_status") or "unknown")
        if status not in {"estimated", "actual", "unknown"}:
            status = "unknown"
        estimated = usage.get("estimated_cost_usd")
        actual = usage.get("actual_cost_usd")
        tokens = (
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_tokens"),
            usage.get("cache_write_tokens"),
            usage.get("reasoning_tokens"),
            usage.get("api_call_count"),
        )
        if status != "unknown":
            cost_source = f"session_model_usage:{profile}"

    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        conn.execute(
            f"INSERT OR IGNORE INTO {LEDGER_TABLE} ({_LEDGER_COLUMNS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(run_id), task_id, board_slug, run["tenant"], run["project_id"],
                profile,
                *tokens,
                estimated if status == "estimated" else None,
                actual if status == "actual" else None,
                status, cost_source,
                period, now, now,
            ),
        )
        # Fill-only-from-unknown: the worker owns the row it was spawned with
        # (run_id PK); a measured row — backfill or an earlier flush — is kept.
        conn.execute(
            f"UPDATE {LEDGER_TABLE} SET "
            "input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, "
            "cache_write_tokens = ?, reasoning_tokens = ?, api_call_count = ?, "
            "estimated_cost_usd = ?, actual_cost_usd = ?, cost_status = ?, "
            "cost_source = ?, board = ?, updated_at = ? "
            "WHERE run_id = ? AND cost_status = 'unknown'",
            (
                *tokens,
                estimated if status == "estimated" else None,
                actual if status == "actual" else None,
                status, cost_source, board_slug, now,
                int(run_id),
            ),
        )
    return True


def worker_run_cost_flush(session_db, session_id: Optional[str]) -> bool:
    """Ledger write for the kanban worker's own run, at process exit (fail-open).

    Runs inside the one-shot session-store flush — after the token-delta drain,
    before ``end_session`` — so every worker exit path (terminal board call,
    block, SIGTERM epilogue) records its run's cost exactly once. Never raises:
    a cost report must never be the cause of a failed run. No-op without the
    dispatcher-stamped ``HERMES_KANBAN_RUN_ID``/``HERMES_KANBAN_TASK`` env pair.
    """
    try:
        run_id_raw = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        if not run_id_raw or not task_id or not session_id:
            return False
        try:
            run_id = int(run_id_raw)
        except ValueError:
            return False
        board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None

        usage = None
        if session_db is not None:
            getter = getattr(session_db, "session_spend_totals", None)
            if callable(getter):
                with contextlib.suppress(Exception):
                    usage = getter(session_id)

        from hermes_cli import kanban_db_connect as _kbc

        conn = _kbc.connect(board=board)
        try:
            return record_worker_run_cost(
                conn, run_id, task_id, board=board, usage=usage,
            )
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    except Exception:
        logger.debug("kanban run-cost flush failed (fail-open)", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Budget CRUD + reads
# ---------------------------------------------------------------------------

def _normalize_ref(scope: str, ref: Optional[str]) -> str:
    if scope == "board":
        return "*"
    value = (ref or "").strip()
    if not value:
        raise ValueError(f"scope {scope!r} requires a non-empty ref (profile name, project id or tenant)")
    return value


def _normalize_period(period: Optional[str], *, monthly: bool) -> str:
    if monthly:
        return PERSIST_PERIOD
    if period:
        if not _PERIOD_RE.match(period):
            raise ValueError(f"period must be 'YYYY-MM', got {period!r}")
        return period
    return current_period()


def set_budget(
    conn,
    *,
    board: str,
    scope: str,
    ref: Optional[str],
    limit_usd: float,
    warn_ratio: float = 0.8,
    period: Optional[str] = None,
    monthly: bool = False,
    created_by: str = "user",
) -> dict[str, Any]:
    """Upsert one budget row (idempotent: re-running replaces the same key)."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}, got {scope!r}")
    try:
        limit = float(limit_usd)
    except (TypeError, ValueError):
        raise ValueError(f"limit must be a number, got {limit_usd!r}") from None
    if not (limit > 0):
        raise ValueError("limit must be > 0 USD")
    try:
        warn = float(warn_ratio)
    except (TypeError, ValueError):
        raise ValueError(f"warn ratio must be a number, got {warn_ratio!r}") from None
    if not (0 < warn <= 1):
        raise ValueError("warn ratio must be in (0, 1]")
    ref_value = _normalize_ref(scope, ref)
    period_value = _normalize_period(period, monthly=monthly)

    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        conn.execute(
            f"INSERT OR REPLACE INTO {BUDGET_TABLE} "
            "(board, scope, ref, period, limit_usd, warn_ratio, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (board, scope, ref_value, period_value, limit, warn, created_by, int(time.time())),
        )
    return {"board": board, "scope": scope, "ref": ref_value,
            "period": period_value, "limit_usd": limit, "warn_ratio": warn}


def remove_budget(
    conn,
    *,
    board: str,
    scope: str,
    ref: Optional[str],
    period: Optional[str] = None,
    monthly: bool = False,
) -> int:
    """Delete one budget row; returns the number of rows removed (0 = absent)."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}, got {scope!r}")
    ref_value = _normalize_ref(scope, ref)
    period_value = _normalize_period(period, monthly=monthly)

    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        cur = conn.execute(
            f"DELETE FROM {BUDGET_TABLE} "
            "WHERE board = ? AND scope = ? AND ref = ? AND period = ?",
            (board, scope, ref_value, period_value),
        )
        return int(cur.rowcount or 0)


def list_budgets(conn, board: str) -> list[dict[str, Any]]:
    """All budget rows of a board (raw rows; ``budget_status`` adds MTD)."""
    rows = conn.execute(
        f"SELECT board, scope, ref, period, limit_usd, warn_ratio, created_by, created_at "
        f"FROM {BUDGET_TABLE} WHERE board = ? ORDER BY scope, ref, period",
        (board,),
    ).fetchall()
    return [
        {
            "scope": r["scope"], "ref": r["ref"], "period": r["period"],
            "limit_usd": float(r["limit_usd"] or 0.0),
            "warn_ratio": float(r["warn_ratio"] or 0.8),
            "created_by": r["created_by"], "created_at": r["created_at"],
        }
        for r in rows
    ]


def mtd_spend(conn, board: str, scope: str, ref: str, period: str) -> float:
    """Month-to-date known spend for one scope (``estimated``/``actual`` rows only).

    Unknown rows deliberately do not count — reinterpreting them as 0 would
    silently understate spend against every limit.
    """
    if scope == "board":
        row = conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) "
            f"FROM {LEDGER_TABLE} "
            "WHERE board = ? AND period = ? AND cost_status IN ('estimated', 'actual')",
            (board, period),
        ).fetchone()
    else:
        column = _SCOPE_COLUMNS[scope]
        row = conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) "
            f"FROM {LEDGER_TABLE} "
            f"WHERE board = ? AND {column} = ? AND period = ? "
            "AND cost_status IN ('estimated', 'actual')",
            (board, ref, period),
        ).fetchone()
    return float(row[0] or 0.0)


def period_usage_stats(conn, board: str, period: str) -> dict[str, Any]:
    """Known MTD spend + unknown coverage for one board-month.

    ``unknown_runs`` counts runs that started in the period and have no measured
    ledger row (no row at all, or ``cost_status='unknown'``) — the honest upper
    bound of unaccounted work, surfaced as ``unknown_share`` in the report.
    """
    mtd = conn.execute(
        f"SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) "
        f"FROM {LEDGER_TABLE} "
        "WHERE board = ? AND period = ? AND cost_status IN ('estimated', 'actual')",
        (board, period),
    ).fetchone()
    stats = conn.execute(
        f"""
        SELECT COUNT(*),
               SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM {LEDGER_TABLE} c
                        WHERE c.run_id = r.id AND c.cost_status != 'unknown')
                    THEN 1 ELSE 0 END)
        FROM task_runs r
        WHERE strftime('%Y-%m', r.started_at, 'unixepoch') = ?
        """,
        (period,),
    ).fetchone()
    runs_total = int(stats[0] or 0)
    unknown = int(stats[1] or 0) if stats[1] is not None else runs_total
    return {
        "mtd_usd": float(mtd[0] or 0.0),
        "runs_total": runs_total,
        "unknown_runs": unknown,
        "unknown_share": (unknown / runs_total) if runs_total else 0.0,
    }


def budget_status(conn, board: str, period: Optional[str] = None) -> dict[str, Any]:
    """Read API for CLI / worker tool: budgets + MTD + unknown share of one month."""
    period_value = period if period else current_period()
    usage = period_usage_stats(conn, board, period_value)
    rows = conn.execute(
        f"SELECT scope, ref, period, limit_usd, warn_ratio FROM {BUDGET_TABLE} "
        "WHERE board = ? AND period IN (?, ?) ORDER BY scope, ref",
        (board, period_value, PERSIST_PERIOD),
    ).fetchall()
    budgets = []
    for r in rows:
        limit = float(r["limit_usd"] or 0.0)
        warn_ratio = float(r["warn_ratio"] or 0.8)
        mtd = mtd_spend(conn, board, r["scope"], r["ref"], period_value)
        state = "stopped" if mtd >= limit else ("warn" if mtd >= limit * warn_ratio else "ok")
        budgets.append({
            "scope": r["scope"], "ref": r["ref"], "period": r["period"],
            "limit_usd": limit, "warn_ratio": warn_ratio,
            "mtd_usd": mtd, "state": state,
        })
    return {
        "board": board, "period": period_value,
        "mtd_usd": usage["mtd_usd"],
        "runs_total": usage["runs_total"],
        "unknown_runs": usage["unknown_runs"],
        "unknown_share": usage["unknown_share"],
        "budgets": budgets,
    }


# ---------------------------------------------------------------------------
# Gate (dispatcher tick, before the claim)
# ---------------------------------------------------------------------------

def _applicable_budgets(conn, board: str, period: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT scope, ref, limit_usd, warn_ratio FROM {BUDGET_TABLE} "
        "WHERE board = ? AND period IN (?, ?)",
        (board, period, PERSIST_PERIOD),
    ).fetchall()
    budgets = [
        {
            "scope": r["scope"], "ref": r["ref"],
            "limit_usd": float(r["limit_usd"] or 0.0),
            "warn_ratio": float(r["warn_ratio"] or 0.8),
        }
        for r in rows
    ]
    # Evaluation priority board → tenant → project → profile: the first violated
    # scope in this order names the event, independent of row storage order.
    budgets.sort(key=lambda b: _SCOPE_PRIORITY.index(b["scope"]) if b["scope"] in _SCOPE_PRIORITY else len(_SCOPE_PRIORITY))
    return budgets


def check_budget(
    conn,
    task_id: str,
    *,
    assignee: Optional[str],
    board: Optional[str],
    now: Optional[int] = None,
) -> dict[str, Optional[dict[str, Any]]]:
    """Pure pre-claim budget check — no writes, so the review-lane mirror in
    ``_any_spawnable_review`` can call it without emitting events.

    Evaluates every applicable budget in board → tenant → project → profile
    priority (each an independent cap). Returns ``{"stop": …, "warn": …}`` where
    each side is ``None`` or ``{scope, ref, period, limit_usd, mtd_usd,
    warn_ratio}``. ``stop`` (any cap's MTD >= limit) wins over ``warn``.
    """
    task = conn.execute(
        "SELECT tenant, project_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None:
        return {"stop": None, "warn": None}
    board = _resolve_board_slug(board)
    period_value = current_period(now=now)
    mtd_cache: dict[tuple[str, str], float] = {}
    stop: Optional[dict[str, Any]] = None
    warn: Optional[dict[str, Any]] = None
    for budget in _applicable_budgets(conn, board, period_value):
        scope, ref = budget["scope"], budget["ref"]
        if scope == "tenant" and (task["tenant"] or "") != ref:
            continue
        if scope == "project" and (task["project_id"] or "") != ref:
            continue
        if scope == "profile" and (assignee or "") != ref:
            continue
        key = (scope, ref)
        if key not in mtd_cache:
            mtd_cache[key] = mtd_spend(conn, board, scope, ref, period_value)
        entry = {
            "scope": scope, "ref": ref, "period": period_value,
            "limit_usd": budget["limit_usd"],
            "warn_ratio": budget["warn_ratio"],
            "mtd_usd": mtd_cache[key],
        }
        if entry["mtd_usd"] >= entry["limit_usd"]:
            stop = entry
            break  # first violated scope in priority order names the event
        if warn is None and entry["mtd_usd"] >= entry["limit_usd"] * entry["warn_ratio"]:
            warn = entry
    return {"stop": stop, "warn": warn}


def _budget_event_exists(conn, task_id: str, kind: str, period: str) -> bool:
    """True when this task already has this budget event for this period.

    Bounded scan (budget events per task are rare); payload-parsed because the
    dedup key lives in the JSON, not in a column.
    """
    from hermes_cli import kanban_db as _kb

    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchall()
    for row in rows:
        payload = _kb._json_or(row["payload"], {})
        if isinstance(payload, dict) and payload.get("period") == period:
            return True
    return False


def _emit_budget_event(
    conn, task_id: str, kind: str, payload: dict[str, Any], *, dry_run: bool,
) -> bool:
    """Append one budget event unless it already exists for (task, period).

    ``dry_run`` reports without writing (mirrors the respawn-guard pattern).
    Returns True when the event was written this call.
    """
    if dry_run or _budget_event_exists(conn, task_id, kind, payload["period"]):
        return False
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        _kb._append_event(conn, task_id, kind, payload)
    return True


def budget_gate(
    conn,
    task_id: str,
    *,
    assignee: Optional[str],
    board: Optional[str],
    lane: str = "ready",
    dry_run: bool = False,
    enabled: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """Dispatcher pre-claim gate. Returns stop info when the spawn must be held.

    With ``enabled`` falsy (flag off / config unreadable) this returns ``None``
    before issuing any query — a disabled gate leaves the tick byte-identical.
    A hard stop is a dispatch gate like ``max_in_progress``: the card stays
    ``ready``, no status change, no block (blocks are human semantics), and it
    re-evaluates every tick against the current limits. A warn crossing emits a
    deduplicated ``budget_warn`` (delivered through the existing notify subs)
    but never holds the spawn.
    """
    if enabled is None:
        enabled = budgets_enabled()
    if not enabled:
        return None
    try:
        decision = check_budget(conn, task_id, assignee=assignee, board=board)
    except Exception:
        logger.debug("kanban budget gate check failed (fail-open)", exc_info=True)
        return None

    stop = decision["stop"]
    warn = decision["warn"]
    if stop is None and warn is None:
        return None

    payload_extra: dict[str, Any] = {}
    if unknown_policy() == "flag":
        with contextlib.suppress(Exception):
            usage = period_usage_stats(conn, board, stop["period"] if stop else warn["period"])
            payload_extra["unknown_share"] = round(usage["unknown_share"], 4)
            payload_extra["unknown_runs"] = usage["unknown_runs"]

    if stop is not None:
        _emit_budget_event(
            conn, task_id, STOP_EVENT_KIND,
            {"reason": "mtd_over_limit", "lane": lane, **stop, **payload_extra},
            dry_run=dry_run,
        )
        return {"kind": STOP_EVENT_KIND, **stop}
    assert warn is not None
    _emit_budget_event(
        conn, task_id, WARN_EVENT_KIND,
        {"reason": "mtd_over_warn_ratio", "lane": lane, **warn, **payload_extra},
        dry_run=dry_run,
    )
    return {"kind": WARN_EVENT_KIND, **warn}


# ---------------------------------------------------------------------------
# CLI surface: ``hermes kanban budget set|show|rm``
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


def dispatch_budget(args: argparse.Namespace) -> int:
    """``hermes kanban budget …`` — the human surface for monthly budgets.

    Workers never route through here for writes: the toolset exposes only the
    read-only ``kanban_budget_show`` (no-self-governance invariant).
    """
    from hermes_cli import kanban_db_connect as _kbc

    action = getattr(args, "budget_action", None) or "show"
    board = _resolve_board_slug(None)
    period_arg = getattr(args, "period", None)

    if action == "set":
        try:
            with _kbc.connect_closing() as conn:
                row = set_budget(
                    conn,
                    board=board,
                    scope=args.scope,
                    ref=args.ref,
                    limit_usd=args.limit,
                    warn_ratio=args.warn,
                    period=period_arg,
                    monthly=args.monthly,
                    created_by=_cli_author(),
                )
        except ValueError as exc:
            return _cli_err(str(exc))
        print(
            f"budget set: {row['scope']} {row['ref']} "
            f"{row['period']} limit ${row['limit_usd']:.2f} "
            f"(warn at {row['warn_ratio']:.0%})"
        )
        return 0

    if action == "rm":
        try:
            with _kbc.connect_closing() as conn:
                removed = remove_budget(
                    conn, board=board, scope=args.scope, ref=args.ref,
                    period=period_arg, monthly=args.monthly,
                )
        except ValueError as exc:
            return _cli_err(str(exc))
        if not removed:
            return _cli_err(
                f"no budget row for {args.scope} {args.ref} "
                f"period {period_arg or ('persist' if args.monthly else current_period())}"
            )
        print(f"budget rm: removed {removed} row(s) for {args.scope} {args.ref}")
        return 0

    if action == "show":
        with _kbc.connect_closing() as conn:
            status = budget_status(conn, board, period_arg)
            # Affected tasks (spec CLI surface): ready cards the CURRENT budget
            # state would hold at the next tick. Pure reads — no events here.
            status["gated_tasks"] = []
            if status["budgets"] and budgets_enabled():
                for ready in conn.execute(
                    "SELECT id, assignee FROM tasks "
                    "WHERE status = 'ready' AND claim_lock IS NULL"
                ).fetchall():
                    decision = check_budget(
                        conn, ready["id"], assignee=ready["assignee"], board=board,
                    )
                    if decision["stop"] is not None:
                        status["gated_tasks"].append(ready["id"])
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0
        print(f"Board: {status['board']}   Period: {status['period']} (UTC)")
        print(
            f"MTD known spend: ${status['mtd_usd']:.2f} across {status['runs_total']} run(s) "
            f"({status['unknown_runs']} unknown = {status['unknown_share']:.0%} unknown share)"
        )
        if not status["budgets"]:
            print("No budgets set (hermes kanban budget set <scope> <ref> --limit USD)")
            return 0
        print(f"\n{'scope':8s} {'ref':20s} {'period':8s} {'limit':>10s} {'MTD':>10s} state")
        for row in status["budgets"]:
            print(
                f"{row['scope']:8s} {row['ref']:20s} {row['period']:8s} "
                f"${row['limit_usd']:>9.2f} ${row['mtd_usd']:>9.2f} {row['state']}"
            )
        if status["gated_tasks"]:
            print(
                f"\nBudget-gated ready tasks (held, not blocked): "
                f"{', '.join(status['gated_tasks'])}"
            )
        return 0

    return _cli_err(f"unknown budget action {action!r}")
