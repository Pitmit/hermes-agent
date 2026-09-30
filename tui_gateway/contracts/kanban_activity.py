"""Read-only Kanban activity projection exposed to shared TUI clients."""

from __future__ import annotations

from pydantic import Field

from .base import Params, Result, WireEnum
from .registry import method


class KanbanTaskStatus(WireEnum):
    triage = "triage"
    todo = "todo"
    scheduled = "scheduled"
    ready = "ready"
    running = "running"
    blocked = "blocked"
    review = "review"
    done = "done"
    archived = "archived"


class KanbanActivityParams(Params):
    boards: list[str] | None = None


class KanbanActivityRun(Result):
    run_id: int | None = None
    profile: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    outcome: str | None = None
    last_heartbeat_at: int | None = None
    max_runtime_seconds: int | None = None
    # Structured progress of the last kanban_heartbeat (governance P1-B1),
    # additive and bounded: None = not reported / unknown (never invented).
    # phase/unit arrive secret-redacted and length-bounded.
    phase: str | None = None
    completed: int | None = None
    total: int | None = None
    unit: str | None = None
    rate: float | None = None
    eta_seconds: int | None = None
    error_count: int | None = None
    progress_pct: int | None = None
    progress_updated_at: int | None = None


class KanbanActivityTask(Result):
    task_id: str
    title: str
    status: KanbanTaskStatus
    assignee: str | None = None
    block_reason: str | None = None
    parents: list[str] = Field(default_factory=list)
    children: list["KanbanActivityTask"] = Field(default_factory=list)
    run: KanbanActivityRun | None = None


class KanbanActivityBoard(Result):
    board: str
    checked_at: int
    roots: list[KanbanActivityTask] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class KanbanActivityResponse(Result):
    boards: list[KanbanActivityBoard] = Field(default_factory=list)
    active_count: int = 0
    attention_count: int = 0
    checked_at: int
    diagnostics: list[str] = Field(default_factory=list)


method(
    "kanban.activity",
    params=KanbanActivityParams,
    result=KanbanActivityResponse,
    doc="Bounded read-only Kanban activity projection for the live TUI dock.",
)
