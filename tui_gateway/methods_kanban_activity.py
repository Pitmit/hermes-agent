"""Bounded read-only Kanban activity RPC for the TUI dock."""

import logging
import time

from .method_ctx import HandlerRegistry, bind_module

logger = logging.getLogger(__name__)
_registry = HandlerRegistry()
method = _registry.method

_KANBAN_ACTIVITY_MAX_BOARDS = 8
_KANBAN_FAILURE_OUTCOMES = frozenset({
    "crashed", "failed", "gave_up", "spawn_failed", "stopped", "timed_out",
})


def _kanban_activity_counts(snapshots: list[dict]) -> tuple[int, int]:
    """Count active/attention tasks without assuming a well-formed tree."""
    active = 0
    attention = 0
    stack = [
        (str(snapshot.get("board") or ""), root)
        for snapshot in reversed(snapshots)
        for root in snapshot.get("roots", [])
    ]
    seen: set[tuple[str, str]] = set()
    while stack:
        board, node = stack.pop()
        if not isinstance(node, dict):
            continue
        key = (board, str(node.get("task_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        status = node.get("status")
        raw_run = node.get("run")
        run: dict = raw_run if isinstance(raw_run, dict) else {}
        if status == "running":
            active += 1
        if status == "blocked" or run.get("outcome") in _KANBAN_FAILURE_OUTCOMES:
            attention += 1
        children = node.get("children")
        if isinstance(children, list):
            stack.extend((board, child) for child in reversed(children))
    return active, attention


@method("kanban.activity")
def _kanban_activity(rid, params: dict) -> dict:
    """Read bounded Kanban activity without touching chat/session delivery."""
    from hermes_cli import kanban_db

    requested = params.get("boards")
    explicit = requested is not None
    diagnostics: list[str] = []
    if explicit:
        if not isinstance(requested, list) or any(not isinstance(board, str) for board in requested):
            return _err(rid, 4000, "boards must be a list of board slugs")  # type: ignore[name-defined]
        canonical: set[str] = set()
        for board in requested:
            candidate = board.strip()
            if not candidate:
                continue
            try:
                candidate = kanban_db.normalize_board_slug(candidate) or ""
            except ValueError:
                candidate = candidate[:64]
            if candidate:
                canonical.add(candidate)
        board_names = sorted(canonical)
        if len(board_names) > _KANBAN_ACTIVITY_MAX_BOARDS:
            diagnostics.append(f"board-limit:{_KANBAN_ACTIVITY_MAX_BOARDS}")
            board_names = board_names[:_KANBAN_ACTIVITY_MAX_BOARDS]
    else:
        pinned = kanban_db.activity_board_scope()
        board_names = [pinned or kanban_db.get_current_board()]

    snapshots: list[dict] = []
    for board in board_names:
        # Ambient polling must never initialize a board as a side effect.
        if not explicit and not kanban_db.kanban_db_path(board=board).exists():
            continue
        try:
            snapshot = kanban_db.get_activity_snapshot(board=board)
        except ValueError:
            snapshot = {
                "board": board[:64],
                "checked_at": int(time.time()),
                "roots": [],
                "error": "invalid board slug",
            }
        except Exception as exc:
            logger.debug("kanban activity read failed for %r: %s", board, exc)
            snapshot = {
                "board": board[:64],
                "checked_at": int(time.time()),
                "roots": [],
                "error": "board unavailable",
            }
        if explicit or snapshot.get("roots") or snapshot.get("error"):
            snapshots.append(snapshot)

    active_count, attention_count = _kanban_activity_counts(snapshots)
    return _ok(rid, {  # type: ignore[name-defined]
        "boards": snapshots,
        "active_count": active_count,
        "attention_count": attention_count,
        "checked_at": int(time.time()),
        "diagnostics": diagnostics,
    })


def register(server):
    bind_module(globals(), server)
