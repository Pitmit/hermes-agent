"""Bounded, read-only Kanban activity projection for TUI/dashboard consumers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

ACTIVITY_MAX_TASKS = 200
ACTIVITY_MAX_GRAPH_SCAN = ACTIVITY_MAX_TASKS * 4
ACTIVITY_MAX_PARENTS_PER_TASK = 16
ACTIVITY_BLOCK_REASON_MAX_CHARS = 240
ACTIVITY_RECENT_COMPLETION_SECONDS = 300


def _board():
    """Resolve the Kanban facade lazily so this module is safe to import first."""
    from hermes_cli import kanban_db

    return kanban_db


def normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Public, side-effect-free board slug canonicalizer."""
    return _board()._normalize_board_slug(slug)


def _bounded_activity_text(value: Any, limit: int) -> Optional[str]:
    """Return a single-line, length-bounded presentation string."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _activity_block_reason(payload: Any) -> Optional[str]:
    """Extract only the documented reason field from a task-event payload."""
    if not payload:
        return None
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    reason = _bounded_activity_text(decoded.get("reason"), ACTIVITY_BLOCK_REASON_MAX_CHARS)
    if reason is None:
        return None
    try:
        from agent.redact import redact_sensitive_text

        reason = redact_sensitive_text(reason, force=True)
    except Exception:
        # This projection is presentation-safe by construction. If the shared
        # redactor cannot load, fail closed rather than exposing arbitrary task
        # text in a composer-adjacent surface.
        return "Details unavailable"
    return _bounded_activity_text(reason, ACTIVITY_BLOCK_REASON_MAX_CHARS)


def activity_board_scope() -> Optional[str]:
    """Return the sole board allowed by a process-level DB pin, if any."""
    if not os.environ.get("HERMES_KANBAN_DB", "").strip():
        return None
    try:
        return _board()._normalize_board_slug(os.environ.get("HERMES_KANBAN_BOARD")) or _board().DEFAULT_BOARD
    except ValueError:
        return _board().DEFAULT_BOARD


def _activity_db_path(board: str) -> Path:
    """Resolve an activity board without letting a DB pin alias another slug."""
    slug = _board()._normalize_board_slug(board)
    if not slug:
        raise ValueError("board slug is required")
    pinned = activity_board_scope()
    if pinned is not None:
        if slug != pinned:
            raise PermissionError(f"board {slug!r} is outside pinned activity scope")
        return Path(os.environ["HERMES_KANBAN_DB"]).expanduser()
    return _board().kanban_db_path(board=slug)


def _open_activity_reader(board: str) -> sqlite3.Connection:
    """Open an existing board database in SQLite read-only mode.

    Unlike :func:`connect`, this never initializes or migrates a database.  A
    presentation poll therefore cannot create a missing board or write schema,
    WAL, task, run, event, or subscription state.
    """
    slug = _board()._normalize_board_slug(board)
    if not slug:
        raise ValueError("board slug is required")
    path = _activity_db_path(slug).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def get_activity_snapshot(*, board: str) -> dict:
    """Return a bounded, deterministic, read-only activity projection.

    The projection is deliberately an allowlist: task body/result/comments,
    run summaries/metadata/errors, process ids, claims, command lines, logs,
    and environment data never enter the returned structure.  Malformed links
    are ignored; cyclic components are rooted deterministically and every task
    is emitted at most once, preventing recursive or exponential expansion.
    """
    slug = _board()._normalize_board_slug(board)
    if not slug:
        raise ValueError("board slug is required")
    checked_at = int(time.time())
    conn = _open_activity_reader(slug)
    try:
        connected_rows = conn.execute(
            """
            WITH RECURSIVE
            seed(id) AS (
                SELECT t.id
                  FROM tasks t
                 WHERE t.status IN ('running', 'blocked')
                    OR EXISTS (
                        SELECT 1
                          FROM task_runs recent
                         WHERE recent.task_id = t.id
                           AND recent.ended_at IS NOT NULL
                           AND recent.ended_at >= ?
                    )
                 ORDER BY t.created_at ASC, t.id ASC
                 LIMIT ?
            ),
            connected(id) AS (
                SELECT id FROM seed
                UNION
                SELECT links.parent_id
                  FROM task_links links
                  JOIN connected current ON links.child_id = current.id
                UNION
                SELECT links.child_id
                  FROM task_links links
                  JOIN connected current ON links.parent_id = current.id
                LIMIT ?
            )
            SELECT id FROM connected
            """,
            (
                checked_at - ACTIVITY_RECENT_COMPLETION_SECONDS,
                ACTIVITY_MAX_TASKS + 1,
                ACTIVITY_MAX_GRAPH_SCAN + 1,
            ),
        ).fetchall()
        scan_truncated = len(connected_rows) > ACTIVITY_MAX_GRAPH_SCAN
        connected_ids = [row["id"] for row in connected_rows[:ACTIVITY_MAX_GRAPH_SCAN]]
        if not connected_ids:
            return {"board": slug, "checked_at": checked_at, "roots": []}

        connected_placeholders = ",".join("?" for _ in connected_ids)
        task_rows = conn.execute(
            f"""
            SELECT id, title, status, assignee, created_at
              FROM tasks
             WHERE id IN ({connected_placeholders})
               AND status != 'archived'
             ORDER BY CASE WHEN status IN ('running', 'blocked') THEN 0 ELSE 1 END,
                      created_at ASC, id ASC
             LIMIT ?
            """,
            [*connected_ids, ACTIVITY_MAX_TASKS + 1],
        ).fetchall()
        truncated = scan_truncated or len(task_rows) > ACTIVITY_MAX_TASKS
        task_rows = task_rows[:ACTIVITY_MAX_TASKS]
        if not task_rows:
            snapshot = {"board": slug, "checked_at": checked_at, "roots": []}
            if truncated:
                snapshot["truncated"] = True
            return snapshot

        task_ids = [row["id"] for row in task_rows]
        id_set = set(task_ids)
        placeholders = ",".join("?" for _ in task_ids)

        link_rows = conn.execute(
            f"""
            SELECT parent_id, child_id, parent_rank
              FROM (
                    SELECT parent_id, child_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY child_id ORDER BY parent_id ASC
                           ) AS parent_rank
                      FROM task_links
                     WHERE child_id IN ({placeholders})
              )
             WHERE parent_rank <= ?
             ORDER BY child_id ASC, parent_rank ASC
            """,
            [*task_ids, ACTIVITY_MAX_PARENTS_PER_TASK + 1],
        ).fetchall()
        if any(row["parent_rank"] > ACTIVITY_MAX_PARENTS_PER_TASK for row in link_rows):
            truncated = True
            link_rows = [
                row
                for row in link_rows
                if row["parent_rank"] <= ACTIVITY_MAX_PARENTS_PER_TASK
            ]

        run_rows = conn.execute(
            f"""
            SELECT r.id, r.task_id, r.profile, r.started_at, r.ended_at,
                   r.outcome, r.last_heartbeat_at, r.max_runtime_seconds
              FROM task_runs r
             WHERE r.task_id IN ({placeholders})
               AND r.id = (
                   SELECT latest.id
                     FROM task_runs latest
                    WHERE latest.task_id = r.task_id
                    ORDER BY latest.started_at DESC, latest.id DESC
                    LIMIT 1
               )
            """,
            task_ids,
        ).fetchall()

        reason_rows = conn.execute(
            f"""
            SELECT e.task_id, e.payload
              FROM task_events e
             WHERE e.task_id IN ({placeholders})
               AND e.kind IN ('blocked', 'dependency_wait', 'block_loop_detected')
               AND e.id = (
                   SELECT latest.id
                     FROM task_events latest
                    WHERE latest.task_id = e.task_id
                      AND latest.kind IN ('blocked', 'dependency_wait', 'block_loop_detected')
                    ORDER BY latest.id DESC
                    LIMIT 1
               )
            """,
            task_ids,
        ).fetchall()
    finally:
        conn.close()

    task_by_id = {row["id"]: row for row in task_rows}
    run_by_task = {row["task_id"]: row for row in run_rows}
    reason_by_task = {
        row["task_id"]: _activity_block_reason(row["payload"])
        for row in reason_rows
    }
    parents: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    children: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    for link in link_rows:
        parent_id = link["parent_id"]
        child_id = link["child_id"]
        if child_id not in id_set:
            continue
        parents[child_id].append(parent_id)
        if parent_id in id_set:
            children[parent_id].append(child_id)

    order = {task_id: index for index, task_id in enumerate(task_ids)}
    fallback_order = len(order)
    for values in parents.values():
        values.sort(key=lambda task_id: (order.get(task_id, fallback_order), task_id))
    for values in children.values():
        values.sort(key=order.__getitem__)

    emitted: set[str] = set()

    def build_node(task_id: str) -> dict:
        emitted.add(task_id)
        task = task_by_id[task_id]
        run = run_by_task.get(task_id)
        node = {
            "task_id": task_id,
            "title": _bounded_activity_text(task["title"], 500) or "Untitled task",
            "status": task["status"],
            "assignee": _bounded_activity_text(task["assignee"], 128),
            "block_reason": reason_by_task.get(task_id),
            "parents": list(parents[task_id]),
            "children": [],
            "run": None,
        }
        if run is not None:
            node["run"] = {
                "run_id": int(run["id"]),
                "profile": _bounded_activity_text(run["profile"], 128),
                "started_at": run["started_at"],
                "ended_at": run["ended_at"],
                "outcome": _bounded_activity_text(run["outcome"], 64),
                "last_heartbeat_at": run["last_heartbeat_at"],
                "max_runtime_seconds": run["max_runtime_seconds"],
            }
        for child_id in children[task_id]:
            if child_id not in emitted:
                node["children"].append(build_node(child_id))
        return node

    root_ids = [task_id for task_id in task_ids if not parents[task_id]]
    roots = [build_node(task_id) for task_id in root_ids if task_id not in emitted]
    # Cycles and nodes whose ancestors fell outside a truncated presentation
    # have no selected natural root. Emit each remaining component once; the
    # explicit parent ids keep the missing edge visible to the renderer.
    roots.extend(build_node(task_id) for task_id in task_ids if task_id not in emitted)
    snapshot = {"board": slug, "checked_at": checked_at, "roots": roots}
    if truncated:
        snapshot["truncated"] = True
    return snapshot
