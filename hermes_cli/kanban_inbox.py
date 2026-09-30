"""Blocked Inbox (governance stage 4) — a read-only projection over blockers.

Spec: docs/kanban-governance-spec.md §6 (Stufe 4). There is NO new table and
NO new mutation path over task state here: the inbox is a pure read endpoint
merging three sources into one deterministically sorted, bounded list:

* ``blocked``  — tasks with ``status='blocked'`` (``block_kind``, last
  ``blocked`` event timestamp as ``blocked_since``);
* ``approval`` — ``approvals`` rows with ``status='pending'`` (stage 2);
* ``review``   — tasks with ``status='review'`` (open review requests).

Derived fields per row:

* ``severity`` — from ``block_kind`` base mapping escalated by stopped age
  relative to the task's effective SLA (spec band ladder, mirroring the
  ``stranded_in_ready`` escalation): age < 2×SLA keeps the base severity,
  2–6×SLA escalates one step, > 6×SLA escalates two steps, capped at
  ``critical``.
* ``action_owner`` — who must act: needs_input → human, capability →
  human-ops, transient → dispatcher-retry, dependency → parent-task,
  pending approvals → human-approver, review → reviewer.
* ``age_seconds`` / ``stopped_age`` — seconds since ``blocked_since`` (the
  ``created_at`` of the last ``blocked`` event). Rows without a ``blocked``
  event (legacy breaker-parked cards) report ``null`` and sort last.

Safety invariants (spec §6):

* The inbox NEVER mutates: no status changes, no events, no drift writes —
  a read appends nothing (unlike ``list_approvals``, it deliberately skips
  the drift refresh, which writes ``approval_events``).
* Human/credential/safety gates stay sticky: nothing here (and nothing in
  the dispatcher/cron) auto-releases ``needs_input``/``capability`` blocks;
  only ``kanban_unblock`` from a human-driven surface or an approval
  decision does. ``block_sla_hours`` is advisory triage metadata only — it
  never gates or releases anything.
* Existing ``block_kind``/reason/recurrence semantics are untouched; the
  sticky-block + ``BLOCK_RECURRENCE_LIMIT`` → triage routing is unchanged.

Setting the per-task SLA override (``set_block_sla``) is the only write in
this module, is fail-closed (hours must be a positive real number), and
appends a ``block_sla_set`` audit event — never a status change.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

# The task card's severity ladder (governance contract): low < medium < high
# < critical. The spec's diagnostics vocabulary maps onto it as
# info→low, warning→medium, error→high, critical→critical.
VALID_SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(VALID_SEVERITIES)}

VALID_INBOX_SOURCES = ("blocked", "approval", "review")

INBOX_DEFAULT_LIMIT = 100
INBOX_MAX_LIMIT = 500

# Board defaults when no config override exists (spec §6): the blocked-SLA
# threshold mirrors ``kanban_diagnostics.DEFAULT_CONFIG["blocked_stale_hours"]``.
DEFAULT_BLOCKED_STALE_HOURS = 24.0
DEFAULT_APPROVAL_STALE_HOURS = 48.0
# One year — anything larger is a typo, not a governance signal.
MAX_SLA_HOURS = 24.0 * 366.0

# Base severity per block kind (spec §6): capability blocks need a human with
# rights (error-class), needs_input/transient are warnings, dependency-waits
# are informational (and dependency blocks never sit in ``blocked`` anyway —
# they wait in ``todo``; kept for completeness and untyped-legacy rows).
_BASE_SEVERITY_BY_KIND = {
    "capability": "high",
    "needs_input": "medium",
    "transient": "medium",
    "dependency": "low",
    None: "medium",  # legacy/untyped sticky human blocker
}

# Who must act to clear the row. Deterministic strings, surfaced verbatim.
_ACTION_OWNER_BY_KIND = {
    "needs_input": "human",
    "capability": "human-ops",
    "transient": "dispatcher-retry",
    "dependency": "parent-task",
    None: "human",
}
_ACTION_OWNER_APPROVAL = "human-approver"
_ACTION_OWNER_REVIEW = "reviewer"

# Review requests age from the latest event that (re)opened the review.
_REVIEW_EVENT_KINDS = ("review_requested", "changes_requested", "review_reopened")


# ---------------------------------------------------------------------------
# Config readers (defaults in code; config.yaml overrides are optional)
# ---------------------------------------------------------------------------


def _kanban_cfg() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        return cfg.get("kanban") if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def board_blocked_sla_hours(kanban_cfg: Optional[dict] = None) -> float:
    """Board-wide blocked SLA in hours (``kanban.diagnostics.blocked_stale_hours``)."""
    cfg = kanban_cfg if kanban_cfg is not None else _kanban_cfg()
    diag = cfg.get("diagnostics") if isinstance(cfg, dict) else None
    value = diag.get("blocked_stale_hours") if isinstance(diag, dict) else None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BLOCKED_STALE_HOURS
    return value if value > 0 else DEFAULT_BLOCKED_STALE_HOURS


def approval_stale_hours(kanban_cfg: Optional[dict] = None) -> float:
    """Pending-approval SLA in hours (``kanban.diagnostics.approval_stale_hours``)."""
    cfg = kanban_cfg if kanban_cfg is not None else _kanban_cfg()
    diag = cfg.get("diagnostics") if isinstance(cfg, dict) else None
    value = diag.get("approval_stale_hours") if isinstance(diag, dict) else None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_STALE_HOURS
    return value if value > 0 else DEFAULT_APPROVAL_STALE_HOURS


def severity_at_or_above(severity: str, threshold: str) -> bool:
    """Filter semantics for ``--severity``: keep rows at or above the threshold."""
    return SEVERITY_RANK[severity] >= SEVERITY_RANK[threshold]


def _escalate(base: str, band: int) -> str:
    """Base severity escalated by ``band`` ladder steps, capped at critical."""
    return VALID_SEVERITIES[min(SEVERITY_RANK[base] + band, SEVERITY_RANK["critical"])]


def _age_band(age_seconds: Optional[float], sla_hours: float) -> int:
    """Spec §6 ladder (mirrors ``stranded_in_ready``): <2×SLA → 0, 2–6× → 1, >6× → 2."""
    if age_seconds is None or sla_hours <= 0:
        return 0
    ratio = age_seconds / (sla_hours * 3600.0)
    if ratio >= 6.0:
        return 2
    if ratio >= 2.0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Derived per-task fields
# ---------------------------------------------------------------------------


def _latest_block_event(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """Newest ``blocked`` event row (created_at + payload reason). None when the
    card was parked ``blocked`` by the failure breaker without a typed block."""
    return conn.execute(
        "SELECT created_at, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _latest_review_since(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute(
        f"SELECT created_at FROM task_events WHERE task_id = ? AND kind IN "
        f"({','.join('?' * len(_REVIEW_EVENT_KINDS))}) ORDER BY id DESC LIMIT 1",
        (task_id, *_REVIEW_EVENT_KINDS),
    ).fetchone()
    return int(row["created_at"]) if row is not None else None


# ---------------------------------------------------------------------------
# The projection (pure read)
# ---------------------------------------------------------------------------


def inbox_rows(
    conn: sqlite3.Connection,
    *,
    board: str,
    severity: Optional[str] = None,
    source: Optional[str] = None,
    block_kind: Optional[str] = None,
    limit: int = INBOX_DEFAULT_LIMIT,
    now: Optional[int] = None,
    kanban_cfg: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """The blocked inbox: blocked tasks + pending approvals + open reviews.

    Deterministically ordered (severity desc, age desc, ref asc) and bounded
    to ``limit``. FAIL-CLOSED on any invalid filter value. Read-only: this
    appends no events and touches no statuses — it must stay usable from any
    cron/watchdog surface without side effects.
    """
    if severity is not None and severity not in VALID_SEVERITIES:
        raise ValueError(f"severity filter must be one of {VALID_SEVERITIES}, got {severity!r}")
    if source is not None and source not in VALID_INBOX_SOURCES:
        raise ValueError(f"source filter must be one of {VALID_INBOX_SOURCES}, got {source!r}")
    if block_kind is not None and block_kind not in kb.VALID_BLOCK_KINDS:
        raise ValueError(
            f"kind filter must be one of {sorted(kb.VALID_BLOCK_KINDS)}, got {block_kind!r}")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > INBOX_MAX_LIMIT:
        raise ValueError(f"limit must be an integer in [1, {INBOX_MAX_LIMIT}], got {limit!r}")

    ts = int(now if now is not None else time.time())
    cfg = kanban_cfg if kanban_cfg is not None else _kanban_cfg()
    board_sla = board_blocked_sla_hours(cfg)
    approval_sla = approval_stale_hours(cfg)
    rows: list[dict[str, Any]] = []

    for task in kb.list_tasks(conn, status="blocked"):
        kind = task.block_kind or None
        event = _latest_block_event(conn, task.id)
        blocked_since = int(event["created_at"]) if event is not None else None
        reason = None
        if event is not None:
            reason = kb._json_dict(event["payload"]).get("reason")
        age = (ts - blocked_since) if blocked_since is not None else None
        sla = float(task.block_sla_hours) if task.block_sla_hours else board_sla
        sev = _escalate(_BASE_SEVERITY_BY_KIND.get(kind, "medium"), _age_band(age, sla))
        rows.append({
            "source": "blocked",
            "ref": task.id,
            "task_id": task.id,
            "title": task.title,
            "assignee": task.assignee,
            "severity": sev,
            "action_owner": _ACTION_OWNER_BY_KIND.get(kind, "human"),
            "age_seconds": age,
            "blocked_since": blocked_since,
            "stopped_age": age,
            "block_kind": kind,
            "block_reason": reason,
            "sla_hours": sla,
            "sla_override": float(task.block_sla_hours) if task.block_sla_hours else None,
        })

    # Pending approvals — plain SELECT on purpose: the drift refresh in
    # ``list_approvals`` writes events, and the inbox must append NOTHING.
    try:
        pending = conn.execute(
            "SELECT id, type, subject_kind, subject_id, requester, requested_at "
            "FROM approvals WHERE board = ? AND status = 'pending'",
            (board,),
        ).fetchall()
    except sqlite3.OperationalError:
        pending = ()  # pre-stage-2 board without the approvals table
    for row in pending:
        requested_at = int(row["requested_at"])
        age = ts - requested_at
        sev = _escalate("medium", _age_band(age, approval_sla))
        rows.append({
            "source": "approval",
            "ref": row["id"],
            "approval_id": row["id"],
            "type": row["type"],
            "subject_kind": row["subject_kind"],
            "subject_id": row["subject_id"],
            "requester": row["requester"],
            "severity": sev,
            "action_owner": _ACTION_OWNER_APPROVAL,
            "age_seconds": age,
            "blocked_since": requested_at,
            "stopped_age": age,
            "sla_hours": approval_sla,
            "sla_override": None,
        })

    for task in kb.list_tasks(conn, status="review"):
        since = _latest_review_since(conn, task.id)
        if since is None and task.started_at:
            since = int(task.started_at)
        age = (ts - since) if since is not None else None
        sev = _escalate("medium", _age_band(age, board_sla))
        rows.append({
            "source": "review",
            "ref": task.id,
            "task_id": task.id,
            "title": task.title,
            "assignee": task.assignee,
            "severity": sev,
            "action_owner": _ACTION_OWNER_REVIEW,
            "age_seconds": age,
            "blocked_since": since,
            "stopped_age": age,
            "sla_hours": board_sla,
            "sla_override": None,
        })

    if severity is not None:
        rows = [r for r in rows if severity_at_or_above(r["severity"], severity)]
    if source is not None:
        rows = [r for r in rows if r["source"] == source]
    if block_kind is not None:
        rows = [r for r in rows if r["source"] == "blocked" and r["block_kind"] == block_kind]

    # Deterministic total order: severity desc, stopped age desc (unknown
    # ages sort last inside their severity), ref asc as the final tiebreak.
    rows.sort(key=lambda r: (
        -SEVERITY_RANK[r["severity"]],
        -(r["age_seconds"] if r["age_seconds"] is not None else -1),
        str(r["ref"]),
    ))
    return rows[:limit]


# ---------------------------------------------------------------------------
# Per-task SLA override (the only write; audited, never a status change)
# ---------------------------------------------------------------------------


def _validated_hours(hours: Any) -> float:
    """Fail-closed: a positive real number, nothing else. Booleans and strings
    are refused even when they "look" numeric — an SLA is not guesswork."""
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        raise ValueError(f"block SLA hours must be a positive number, got {hours!r}")
    value = float(hours)
    if not (0 < value <= MAX_SLA_HOURS):
        raise ValueError(
            f"block SLA hours must be in (0, {MAX_SLA_HOURS:g}], got {value:g}")
    return value


def set_block_sla(
    conn: sqlite3.Connection, task_id: str, hours: Any, *,
    clear: bool = False, actor: Optional[str] = None,
) -> dict[str, Any]:
    """Set (``hours``) or clear (``clear=True``) the per-task blocked-SLA override.

    Advisory triage metadata only: never gates, never releases, never touches
    status. Fails closed on invalid input; appends a ``block_sla_set`` audit
    event so a human can see who moved the SLA and from what.
    """
    with kbc.write_txn(conn):
        row = conn.execute(
            "SELECT block_sla_hours FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no such task: {task_id}")
        previous = row["block_sla_hours"]
        new_value = None if clear else _validated_hours(hours)
        conn.execute(
            "UPDATE tasks SET block_sla_hours = ? WHERE id = ?", (new_value, task_id),
        )
        kb._append_event(conn, task_id, "block_sla_set", {
            "hours": new_value, "previous": previous,
            "actor": actor or kb._claimer_id(),
        })
    return {
        "task_id": task_id,
        "block_sla_hours": new_value,
        "previous": previous,
        "cleared": bool(clear),
    }


# ---------------------------------------------------------------------------
# CLI surface: ``hermes kanban inbox`` / ``hermes kanban block-sla``
# ---------------------------------------------------------------------------


def _cli_err(message: str) -> int:
    print(f"kanban: {message}")
    return 1


def _fmt_age(age_seconds: Optional[int]) -> str:
    if age_seconds is None:
        return "unknown"
    seconds = int(age_seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{max(seconds % 3600 // 60, 0):02d}m"
    return f"{seconds // 86400}d{max(seconds % 86400 // 3600, 0):02d}h"


def dispatch_inbox(args: argparse.Namespace) -> int:
    """``hermes kanban inbox`` — read-only blocked inbox (also ``/kanban inbox``)."""
    board = kb.get_current_board() or kb.DEFAULT_BOARD
    as_json = bool(getattr(args, "json", False))
    try:
        with kbc.connect_closing() as conn:
            # `--limit 0` must FAIL, not fall back to the default (fail-closed).
            limit_arg = getattr(args, "limit", None)
            rows = inbox_rows(
                conn,
                board=board,
                severity=getattr(args, "severity", None),
                source=getattr(args, "source", None),
                block_kind=getattr(args, "kind", None),
                limit=limit_arg if limit_arg is not None else INBOX_DEFAULT_LIMIT,
            )
    except ValueError as exc:
        return _cli_err(str(exc))
    if as_json:
        print(json.dumps({"board": board, "count": len(rows), "rows": rows},
                         indent=2, sort_keys=True))
        return 0
    if not rows:
        print(f"Blocked inbox for board {board} is empty — no blocked tasks, "
              "no pending approvals, no open review requests.")
        return 0
    print(f"Board: {board}   ({len(rows)} inbox row(s))")
    print(f"{'severity':9s} {'age':8s} {'source':9s} {'action owner':16s} ref / subject")
    for r in rows:
        ref = r.get("task_id") or r.get("approval_id")
        detail = r.get("title") or f"{r.get('subject_kind')}:{r.get('subject_id')}"
        print(f"{r['severity']:9s} {_fmt_age(r['age_seconds']):8s} "
              f"{r['source']:9s} {r['action_owner']:16s} {ref} — {detail}")
    return 0


def dispatch_block_sla(args: argparse.Namespace) -> int:
    """``hermes kanban block-sla <task_id>`` — set/show/clear the per-task SLA."""
    task_id = args.task_id
    as_json = bool(getattr(args, "json", False))
    hours = getattr(args, "hours", None)
    clear = bool(getattr(args, "clear", False))
    if hours is not None and clear:
        return _cli_err("pass either --hours or --clear, not both")
    try:
        with kbc.connect_closing() as conn:
            if hours is None and not clear:
                task = kb.get_task(conn, task_id)
                if task is None:
                    return _cli_err(f"no such task: {task_id}")
                info = {
                    "task_id": task_id,
                    "block_sla_hours": task.block_sla_hours,
                    "effective_sla_hours": (
                        float(task.block_sla_hours) if task.block_sla_hours
                        else board_blocked_sla_hours()
                    ),
                }
                if as_json:
                    print(json.dumps(info, indent=2, sort_keys=True))
                else:
                    override = task.block_sla_hours
                    eff = info["effective_sla_hours"]
                    src = "per-task override" if override else "board default"
                    print(f"{task_id}: blocked-SLA = {eff:g} h ({src})")
                return 0
            result = set_block_sla(conn, task_id, hours, clear=clear)
    except (ValueError, LookupError) as exc:
        return _cli_err(str(exc))
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    value = "cleared (board default applies)" if result["cleared"] else f"{result['block_sla_hours']:g} h"
    print(f"{task_id}: blocked-SLA set to {value} (was {result['previous']})")
    return 0
