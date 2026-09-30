"""Kanban workflow templates (governance stage 6, P1-A3).

Spec: ``docs/kanban-governance-spec.md`` §8 (Stufe 6) — activate the dormant
``tasks.workflow_template_id`` / ``current_step_key`` columns with
user-definable, linear workflow templates. Three surfaces, one kernel:

* **Kernel** — :func:`create_template` / :func:`get_template` /
  :func:`list_templates` (the ``kanban_workflow_templates`` table, seeded with
  five reference templates as DATA — no per-template code paths), and
  :func:`apply_workflow_template`, which creates the WHOLE step chain
  atomically: one ``write_txn`` containing every step card and every
  ``task_links`` edge, so the dispatcher can never observe a partial graph
  (no child-claim race) and a rejected application leaves no half chain.
* **CLI** — :func:`dispatch_workflow_template` for ``hermes kanban
  workflow-template create|list|show``; applying rides on ``hermes kanban
  create --workflow-template <id> --role ROLE=PROFILE …``.
* **Worker tool** — ``kanban_create(workflow_template_id=…, roles=…)``
  (tools/kanban_tools.py) fans the same kernel out to orchestrator workers.

Design contract (fail-closed, spec §8 "Sicherheitsinvarianten"):

- Templates are DATA: linear step lists only — no DSL, no parallel branches,
  no conditional jumps. The parent gate IS the routing engine. A repeated
  ``step_key`` is the only cycle a linear list can express and is rejected
  (cycle/self-reference guard).
- Steps carry ROLE names (``assignee`` field), not concrete profiles. Applying
  resolves every role through a caller-supplied ``role_map`` and validates the
  RESOLVED profiles (existence), forced skills (assignee-profile resolvable)
  and remote workspaces (the same fail-closed rules as plain ``kanban_create``)
  BEFORE a single row is written: an impossible application is rejected
  without a partial graph.
- ``review`` and ``approval_type`` are template metadata carried onto the step
  card body — the step card itself is the gate (the next step is parent-gated
  on its completion). A template never decides or approves anything by itself;
  human decisions keep flowing through the existing review/approval surfaces.
- Idempotency: an ``idempotency_key`` deduplicates the whole application —
  a retry (lost tool response) reads the same chain back instead of
  duplicating it. Each step card also derives its own key so a concurrent
  double-apply cannot interleave partial chains (the outer ``BEGIN IMMEDIATE``
  plus the in-transaction re-check serializes appliers).
- Audit: every application appends exactly one ``workflow_applied`` event on
  the step-1 card carrying the resolved graph (template, step keys, task ids,
  role map); the per-card ``created`` events carry
  ``workflow_template_id``/``current_step_key``.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc

WORKFLOW_TEMPLATE_TABLE = "kanban_workflow_templates"

# Template/step identity: kebab-case words after the ``wf_`` / bare prefix.
_TEMPLATE_ID_RE = re.compile(r"^wf_[a-z0-9][a-z0-9-]{0,63}$")
_STEP_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_STEP_FIELDS = frozenset({
    "step_key", "title", "assignee", "skills", "workspace_kind", "workspace_path",
    "remote_workspace_verified", "priority", "review", "approval_type", "body",
})


class WorkflowTemplateError(ValueError):
    """Invalid template definition or application (fail-closed, pre-persist)."""


def _resolve_board_slug(board: Optional[str]) -> str:
    return board or _kb.get_current_board() or _kb.DEFAULT_BOARD


def _require_text(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowTemplateError(f"{what} is required")
    return text


# ---------------------------------------------------------------------------
# Step / template model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowStep:
    """One validated template step (roles, not concrete profiles)."""

    step_key: str
    title: str
    assignee: str                 # ROLE name; resolved at apply time
    skills: Optional[tuple] = None
    workspace_kind: Optional[str] = None
    workspace_path: Optional[str] = None
    remote_workspace_verified: bool = False
    priority: Optional[int] = None
    review: bool = False
    approval_type: Optional[str] = None
    body: Optional[str] = None


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    name: str
    steps: tuple
    created_by: str
    created_at: int
    board: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "board": self.board,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "steps": [
                {
                    "step_key": s.step_key, "title": s.title, "assignee": s.assignee,
                    "skills": list(s.skills) if s.skills else None,
                    "workspace_kind": s.workspace_kind, "workspace_path": s.workspace_path,
                    "remote_workspace_verified": s.remote_workspace_verified or None,
                    "priority": s.priority, "review": s.review or None,
                    "approval_type": s.approval_type, "body": s.body,
                }
                for s in self.steps
            ],
        }


@dataclass(frozen=True)
class WorkflowApplied:
    """Result of one application (fresh or deduplicated)."""

    root_task_id: str
    template_id: str
    template_name: str
    step_task_ids: tuple = field(default_factory=tuple)
    step_keys: tuple = field(default_factory=tuple)
    deduplicated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_name": self.template_name,
            "root_task_id": self.root_task_id,
            "steps": [
                {"step_key": k, "task_id": t}
                for k, t in zip(self.step_keys, self.step_task_ids)
            ],
            "deduplicated": self.deduplicated,
        }


def _validate_approval_type(value: Any) -> Optional[str]:
    if value is None:
        return None
    from hermes_cli.kanban_approvals import VALID_APPROVAL_TYPES

    text = str(value).strip()
    if text not in VALID_APPROVAL_TYPES:
        raise WorkflowTemplateError(
            f"step approval_type must be one of {sorted(VALID_APPROVAL_TYPES)} or null, "
            f"got {value!r}")
    return text


def parse_workflow_steps(raw: Any) -> tuple:
    """Validate a raw steps payload into :class:`WorkflowStep` objects.

    Linear list, no duplicates (a repeated ``step_key`` is the only cycle a
    linear chain can express — rejected here), roles instead of profiles.
    """
    if not isinstance(raw, list) or not raw:
        raise WorkflowTemplateError(
            "steps must be a non-empty JSON list of step objects")
    steps: list[WorkflowStep] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise WorkflowTemplateError(f"steps[{i}] must be a JSON object, got {type(item).__name__}")
        unknown = sorted(set(item) - _STEP_FIELDS)
        if unknown:
            raise WorkflowTemplateError(
                f"steps[{i}] has unknown field(s) {', '.join(unknown)}; "
                f"valid fields: {', '.join(sorted(_STEP_FIELDS))}")
        step_key = _require_text(item.get("step_key"), f"steps[{i}].step_key").lower()
        if not _STEP_KEY_RE.match(step_key):
            raise WorkflowTemplateError(
                f"steps[{i}].step_key {step_key!r} must match {_STEP_KEY_RE.pattern}")
        if step_key in seen:
            # Cycle guard: a linear chain reaches a step_key at most once.
            raise WorkflowTemplateError(
                f"step_key {step_key!r} appears more than once — a workflow "
                "chain must be linear (no cycles, no self-reference)")
        seen.add(step_key)
        title = _require_text(item.get("title"), f"steps[{i}].title")
        role = _require_text(item.get("assignee"), f"steps[{i}].assignee (role name)").lower()
        if not _ROLE_RE.match(role):
            raise WorkflowTemplateError(
                f"steps[{i}].assignee (role) {role!r} must match {_ROLE_RE.pattern}")
        skills_raw = item.get("skills")
        if skills_raw is not None:
            if not isinstance(skills_raw, list) or not all(
                    isinstance(s, str) and s.strip() for s in skills_raw):
                raise WorkflowTemplateError(
                    f"steps[{i}].skills must be a list of non-empty skill names")
        skills = tuple(dict.fromkeys(str(s).strip() for s in skills_raw)) if skills_raw else None
        workspace_kind = item.get("workspace_kind")
        if workspace_kind is not None and workspace_kind not in _kb.VALID_WORKSPACE_KINDS:
            raise WorkflowTemplateError(
                f"steps[{i}].workspace_kind must be one of "
                f"{sorted(_kb.VALID_WORKSPACE_KINDS)}, got {workspace_kind!r}")
        workspace_path = item.get("workspace_path")
        if workspace_path is not None:
            workspace_path = str(workspace_path).strip() or None
        priority = item.get("priority")
        if priority is not None:
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                raise WorkflowTemplateError(f"steps[{i}].priority must be an integer") from None
            if priority < 0:
                raise WorkflowTemplateError(f"steps[{i}].priority must be >= 0")
        review = item.get("review")
        if review is not None and not isinstance(review, bool):
            raise WorkflowTemplateError(f"steps[{i}].review must be a boolean")
        steps.append(WorkflowStep(
            step_key=step_key, title=title, assignee=role, skills=skills,
            workspace_kind=workspace_kind, workspace_path=workspace_path,
            remote_workspace_verified=bool(item.get("remote_workspace_verified")),
            priority=priority, review=bool(review),
            approval_type=_validate_approval_type(item.get("approval_type")),
            body=(str(item["body"]).strip() or None) if item.get("body") else None,
        ))
    return tuple(steps)


# ---------------------------------------------------------------------------
# Reference template set (DATA — spec §8: "Auslieferung als Daten, nicht Code")
# ---------------------------------------------------------------------------

_REFERENCE_TEMPLATES: tuple[dict, ...] = (
    {
        "id": "wf_research-synthesis-review",
        "name": "Research → Synthesis → Review",
        "steps": (
            {"step_key": "research", "title": "Research the question",
             "assignee": "researcher"},
            {"step_key": "synthesis", "title": "Synthesize the findings",
             "assignee": "synthesizer"},
            {"step_key": "review", "title": "Review the synthesis",
             "assignee": "reviewer", "review": True},
        ),
    },
    {
        "id": "wf_recon-implement-review",
        "name": "Recon → Implement → Review",
        "steps": (
            {"step_key": "recon", "title": "Recon: verify premises live",
             "assignee": "recon"},
            {"step_key": "implement", "title": "Implement the change",
             "assignee": "implementer"},
            {"step_key": "review", "title": "Review the implementation",
             "assignee": "reviewer", "review": True},
        ),
    },
    {
        "id": "wf_implement-publish-gate-approve-merge",
        "name": "Implement → Publish Gate → Approve → Merge",
        "steps": (
            {"step_key": "implement", "title": "Implement the change",
             "assignee": "implementer"},
            {"step_key": "publish_gate", "title": "Prepare the publish gate (build, changelog, version)",
             "assignee": "publisher"},
            {"step_key": "approve", "title": "Approve the release (human gate)",
             "assignee": "approver", "approval_type": "release"},
            {"step_key": "merge", "title": "Merge the approved release",
             "assignee": "merger"},
        ),
    },
    {
        "id": "wf_incident-rca-repair-verify",
        "name": "Incident → RCA → Repair → Verify",
        "steps": (
            {"step_key": "incident", "title": "Acknowledge and stabilize the incident",
             "assignee": "oncall"},
            {"step_key": "rca", "title": "Root-cause analysis",
             "assignee": "analyst"},
            {"step_key": "repair", "title": "Repair the root cause",
             "assignee": "implementer"},
            {"step_key": "verify", "title": "Verify the fix end to end",
             "assignee": "verifier", "review": True},
        ),
    },
    {
        "id": "wf_migration-recon-snapshot-execute-userpath-accept",
        "name": "Migration → Recon → Snapshot → Execute → User Path → Accept",
        "steps": (
            {"step_key": "migration", "title": "Plan the migration (scope, rollback, comms)",
             "assignee": "planner"},
            {"step_key": "recon", "title": "Recon the source system",
             "assignee": "recon"},
            {"step_key": "snapshot", "title": "Take a restorable snapshot",
             "assignee": "implementer"},
            {"step_key": "execute", "title": "Execute the migration",
             "assignee": "implementer"},
            {"step_key": "userpath", "title": "Exercise the real user path end to end",
             "assignee": "verifier"},
            {"step_key": "accept", "title": "Accept the migration or roll back",
             "assignee": "owner", "review": True},
        ),
    },
)


def ensure_reference_templates(conn: sqlite3.Connection, board: Optional[str] = None) -> int:
    """Idempotently seed the five reference templates (``INSERT OR IGNORE``).

    Called from ``kanban_db_connect._init_if_needed`` — once per process per
    board path, right after the schema/migration pass. A user-redefined row
    with a reference id is preserved (OR IGNORE); templates are data, boards
    own their copy.
    """
    board_slug = _resolve_board_slug(board)
    now = int(time.time())
    with _kbc.write_txn(conn):
        for tpl in _REFERENCE_TEMPLATES:
            cur = conn.execute(
                f"SELECT 1 FROM {WORKFLOW_TEMPLATE_TABLE} WHERE board = ? AND id = ?",
                (board_slug, tpl["id"]),
            ).fetchone()
            if cur is not None:
                continue
            conn.execute(
                f"INSERT OR IGNORE INTO {WORKFLOW_TEMPLATE_TABLE} "
                "(board, id, name, steps, created_by, created_at) VALUES (?,?,?,?,?,?)",
                (board_slug, tpl["id"], tpl["name"],
                 json.dumps(list(tpl["steps"])), "reference-set", now),
            )
    return len(_REFERENCE_TEMPLATES)


# ---------------------------------------------------------------------------
# Template CRUD
# ---------------------------------------------------------------------------


def _template_from_row(row: sqlite3.Row, board: str) -> WorkflowTemplate:
    try:
        raw_steps = json.loads(row["steps"])
    except (TypeError, ValueError):
        raise WorkflowTemplateError(
            f"workflow template {row['id']!r} has an unparseable steps payload") from None
    return WorkflowTemplate(
        id=row["id"], name=row["name"], steps=parse_workflow_steps(raw_steps),
        created_by=row["created_by"], created_at=int(row["created_at"] or 0), board=board,
    )


def create_template(
    conn: sqlite3.Connection, *, board: Optional[str], name: str,
    steps: Any, template_id: Optional[str] = None, created_by: str = "user",
) -> WorkflowTemplate:
    """Create a user-definable template (validated fail-closed before insert)."""
    board_slug = _resolve_board_slug(board)
    tpl_id = (template_id or "").strip() or ("wf_" + secrets.token_hex(4))
    if not _TEMPLATE_ID_RE.match(tpl_id):
        raise WorkflowTemplateError(
            f"template id {tpl_id!r} must match {_TEMPLATE_ID_RE.pattern} (e.g. wf_my-flow)")
    clean_name = _require_text(name, "template name")
    parsed = parse_workflow_steps(steps)
    existing = conn.execute(
        f"SELECT id FROM {WORKFLOW_TEMPLATE_TABLE} WHERE board = ? AND id = ?",
        (board_slug, tpl_id),
    ).fetchone()
    if existing is not None:
        raise WorkflowTemplateError(
            f"workflow template {tpl_id!r} already exists on board {board_slug!r}")
    now = int(time.time())
    with _kbc.write_txn(conn):
        conn.execute(
            f"INSERT INTO {WORKFLOW_TEMPLATE_TABLE} "
            "(board, id, name, steps, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (board_slug, tpl_id, clean_name, json.dumps(steps, ensure_ascii=False),
             created_by, now),
        )
    return WorkflowTemplate(
        id=tpl_id, name=clean_name, steps=parsed, created_by=created_by,
        created_at=now, board=board_slug,
    )


def get_template(
    conn: sqlite3.Connection, *, board: Optional[str], template_id: str,
) -> Optional[WorkflowTemplate]:
    board_slug = _resolve_board_slug(board)
    row = conn.execute(
        f"SELECT * FROM {WORKFLOW_TEMPLATE_TABLE} WHERE board = ? AND id = ?",
        (board_slug, str(template_id).strip()),
    ).fetchone()
    return _template_from_row(row, board_slug) if row is not None else None


def list_templates(conn: sqlite3.Connection, *, board: Optional[str]) -> list:
    board_slug = _resolve_board_slug(board)
    rows = conn.execute(
        f"SELECT * FROM {WORKFLOW_TEMPLATE_TABLE} WHERE board = ? ORDER BY id",
        (board_slug,),
    ).fetchall()
    return [_template_from_row(r, board_slug) for r in rows]


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _validate_role_map(
    template: WorkflowTemplate, role_map: Mapping[str, str],
) -> dict:
    """Resolve roles → profiles; every used role must be mapped (fail-closed)."""
    if not isinstance(role_map, Mapping):
        raise WorkflowTemplateError(
            "roles must be an object mapping role names to profile names")
    resolved: dict[str, str] = {}
    for step in template.steps:
        role = step.assignee
        if role in resolved:
            continue
        profile = role_map.get(role)
        if not isinstance(profile, str) or not profile.strip():
            raise WorkflowTemplateError(
                f"step {step.step_key!r} needs role {role!r} but no profile is mapped "
                f"for it (pass --role {role}=<profile> / roles.{'{'}\"{role}\": \"<profile>\"{'}'})")
        resolved[role] = profile.strip()
    return resolved


def validate_workflow_application(
    template: WorkflowTemplate, *, role_map: Mapping[str, str], board: Optional[str],
) -> list:
    """Full fail-closed pre-persist validation; returns [(step, profile), …].

    Profile existence, forced skills and remote workspaces are validated for
    EVERY step before a single row is written, using the exact same rules as a
    plain ``kanban_create`` (late import — the tool layer owns the resolvers).
    """
    from tools.kanban_tools import (
        _Reject, _validate_assignee_skills, _validate_remote_dir_workspace,
    )

    def _human(reject: _Reject) -> str:
        # ``_Reject`` carries a finished tool_error JSON payload — surface the
        # human message, never the wrapper.
        try:
            return str(json.loads(str(reject.args[0])).get("error") or reject.args[0])
        except Exception:
            return str(reject.args[0])

    resolved = _validate_role_map(template, role_map)
    pairs = []
    for step in template.steps:
        profile = resolved[step.assignee]
        from hermes_cli.profiles import profile_exists

        if not profile_exists(profile):
            from hermes_cli.profiles import list_profile_names

            raise WorkflowTemplateError(
                f"step {step.step_key!r}: assignee profile {profile!r} is not installed. "
                f"Installed profiles: {', '.join(list_profile_names()) or '(none)'}")
        try:
            _validate_assignee_skills(profile, list(step.skills) if step.skills else None)
            _validate_remote_dir_workspace(
                profile, step.workspace_kind, step.workspace_path,
                remote_workspace_verified=step.remote_workspace_verified, board=board,
            )
        except _Reject as exc:
            raise WorkflowTemplateError(
                f"step {step.step_key!r}: {_human(exc)}") from exc
        pairs.append((step, profile))
    return pairs


def _step_body(
    template: WorkflowTemplate, index: int, total: int, step: WorkflowStep,
    profile: str, context_body: Optional[str],
) -> str:
    parts = []
    if context_body:
        parts.append(str(context_body).strip())
    parts.append(
        f"Workflow: {template.name} ({template.id}) — step {index}/{total}: "
        f"{step.step_key} (role {step.assignee} → profile {profile})."
    )
    parts.append(
        "This card is one step of a linear workflow chain: the next step "
        "unlocks when this card completes."
    )
    if index > 1:
        parts.append(
            "The previous step's handoff arrives in this card's parent results "
            "(`kanban show` context)."
        )
    if step.review:
        parts.append(
            "REVIEW GATE: this step is the review gate of the workflow — review "
            "the previous step's output before completing this card."
        )
    if step.approval_type:
        parts.append(
            f"APPROVAL GATE: this step requires a human approval of type "
            f"'{step.approval_type}' (governance stage 2) before completion — "
            "request it; a template never approves on its own."
        )
    if step.body:
        parts.append(str(step.body).strip())
    return "\n\n".join(parts)


def _find_root_by_key(conn: sqlite3.Connection, idempotency_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    return row["id"] if row is not None else None


def _recover_chain(
    conn: sqlite3.Connection, root_id: str, template: WorkflowTemplate,
) -> dict:
    """Walk the applied chain below ``root_id``: {step_key: task_id}.

    Exactly one template-scoped child per node; a partial or branching chain
    (impossible via the atomic apply, so a corruption signal) fails closed.
    """
    chain = {}
    node = root_id
    root = _kb.get_task(conn, root_id)
    if root is None or root.workflow_template_id != template.id:
        raise WorkflowTemplateError(
            f"idempotency key is already bound to task {root_id!r}, which is not "
            f"the first card of workflow template {template.id!r}")
    chain[root.current_step_key] = root_id
    while True:
        rows = conn.execute(
            "SELECT l.child_id AS child, t.current_step_key AS step_key "
            "FROM task_links l JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ? AND t.workflow_template_id = ?",
            (node, template.id),
        ).fetchall()
        if not rows:
            break
        if len(rows) != 1:
            raise WorkflowTemplateError(
                f"workflow chain below {node!r} is not linear "
                f"({len(rows)} template children) — refusing to deduplicate a "
                "corrupted chain")
        node = rows[0]["child"]
        key = rows[0]["step_key"]
        if key in chain:
            raise WorkflowTemplateError(
                f"workflow chain below {root_id!r} repeats step {key!r}")
        chain[key] = node
    expected = {s.step_key for s in template.steps}
    found = set(chain)
    if found != expected:
        raise WorkflowTemplateError(
            f"workflow chain for {template.id!r} under {root_id!r} does not match "
            f"the template (found {sorted(found)}, expected {sorted(expected)}) — "
            "refusing to deduplicate a partial chain")
    return chain


def apply_workflow_template(
    conn: sqlite3.Connection, *, template_id: str, title: str,
    role_map: Mapping[str, str], board: Optional[str] = None,
    body: Optional[str] = None, tenant: Optional[str] = None, priority: int = 0,
    created_by: Optional[str] = None, session_id: Optional[str] = None,
    idempotency_key: Optional[str] = None, extra_parents: Iterable[str] = (),
    creator_task_id: Optional[str] = None,
) -> WorkflowApplied:
    """Create the full step chain of ``template_id`` in ONE transaction.

    Steps 2..N are parent-gated on their predecessor via the existing
    ``task_links``/promote machinery (no new routing engine); a lost-response
    retry with the same ``idempotency_key`` reads the existing chain back
    (``deduplicated=True``) instead of duplicating it.
    """
    clean_title = _require_text(title, "workflow instance title")
    template = get_template(conn, board=board, template_id=template_id)
    if template is None:
        raise WorkflowTemplateError(
            f"workflow template {str(template_id).strip()!r} does not exist on board "
            f"{_resolve_board_slug(board)!r} (see `hermes kanban workflow-template list`)")

    # FULL fail-closed validation BEFORE any write (no partial graph on reject).
    pairs = validate_workflow_application(template, role_map=role_map, board=board)

    def _dedupe_path(root_id: str) -> WorkflowApplied:
        chain = _recover_chain(conn, root_id, template)
        return WorkflowApplied(
            root_task_id=root_id, template_id=template.id, template_name=template.name,
            step_task_ids=tuple(chain[s.step_key] for s in template.steps),
            step_keys=tuple(s.step_key for s in template.steps), deduplicated=True,
        )

    if idempotency_key:
        existing = _find_root_by_key(conn, idempotency_key)
        if existing is not None:
            return _dedupe_path(existing)

    step_keys = tuple(s.step_key for s in template.steps)
    with _kbc.write_txn(conn):
        # In-transaction re-check closes the concurrent double-apply race: the
        # BEGIN IMMEDIATE lock serializes appliers, the second one lands here.
        if idempotency_key:
            existing = _find_root_by_key(conn, idempotency_key)
            if existing is not None:
                chain = _recover_chain(conn, existing, template)
                return WorkflowApplied(
                    root_task_id=existing, template_id=template.id,
                    template_name=template.name,
                    step_task_ids=tuple(chain[s.step_key] for s in template.steps),
                    step_keys=step_keys, deduplicated=True,
                )
        root_id: Optional[str] = None
        prev_id: Optional[str] = None
        step_ids: list[str] = []
        resolved_roles = {s.assignee: p for s, p in pairs}
        for index, (step, profile) in enumerate(pairs, start=1):
            # Linear chain: each step is parent-gated on its PREDECESSOR; only
            # step 1 can carry the caller's extra_parents. Step 1 keeps the
            # caller's idempotency key (root lookup/dedupe), steps 2..N derive
            # their own traceable keys.
            parents = (prev_id,) if prev_id is not None else tuple(p for p in extra_parents if p)
            step_key_value = (
                idempotency_key if not idempotency_key or index == 1
                else f"{idempotency_key}::wf:{template.id}:{step.step_key}"
            )
            tid = _kb.create_task(
                conn,
                title=f"{clean_title} — {step.title}",
                body=_step_body(template, index, len(pairs), step, profile, body),
                assignee=profile, parents=parents, tenant=tenant,
                priority=(step.priority if step.priority is not None else priority),
                workspace_kind=step.workspace_kind, workspace_path=step.workspace_path,
                skills=list(step.skills) if step.skills else None,
                created_by=created_by, session_id=session_id, board=board,
                idempotency_key=step_key_value,
                workflow_template_id=template.id, current_step_key=step.step_key,
                creator_task_id=creator_task_id,
            )
            if root_id is None:
                root_id = tid
            prev_id = tid
            step_ids.append(tid)
        _kb._append_event(
            conn, root_id, "workflow_applied",
            {
                "template_id": template.id, "template_name": template.name,
                "step_keys": list(step_keys),
                "steps": [
                    {"step_key": s.step_key, "task_id": t, "assignee": p}
                    for s, t, p in zip(template.steps, step_ids,
                                       [pr for _, pr in pairs])
                ],
                "roles": resolved_roles,
                "idempotency_key": idempotency_key,
            },
        )
    return WorkflowApplied(
        root_task_id=root_id, template_id=template.id, template_name=template.name,
        step_task_ids=tuple(step_ids), step_keys=step_keys, deduplicated=False,
    )


# ---------------------------------------------------------------------------
# CLI surface (`hermes kanban workflow-template …`)
# ---------------------------------------------------------------------------


def _parse_role_arg(raw: str) -> tuple[str, str]:
    role, sep, profile = str(raw).partition("=")
    if not sep or not role.strip() or not profile.strip():
        raise argparse.ArgumentTypeError(
            "role mapping must be ROLE=PROFILE (e.g. --role reviewer=reviewer-a)")
    return role.strip().lower(), profile.strip()


def dispatch_workflow_template(args: argparse.Namespace) -> int:
    """``hermes kanban workflow-template create|list|show``."""
    action = getattr(args, "workflow_template_action", None) or "list"
    board = _resolve_board_slug(getattr(args, "board", None))
    as_json = bool(getattr(args, "json", False))

    if action == "create":
        steps_raw = getattr(args, "steps", None)
        if not steps_raw:
            print("kanban: --steps is required (JSON list or @file)", file=sys.stderr)
            return 2
        spec = str(steps_raw)
        try:
            if spec.startswith("@"):
                spec = Path(spec[1:]).read_text(encoding="utf-8-sig")
            steps = json.loads(spec)
        except (OSError, ValueError) as exc:
            print(f"kanban: --steps: {exc}", file=sys.stderr)
            return 2
        try:
            with _kbc.connect_closing() as conn:
                tpl = create_template(
                    conn, board=board, name=args.name, steps=steps,
                    template_id=getattr(args, "template_id", None),
                    created_by=_acting_profile(),
                )
        except WorkflowTemplateError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(tpl.as_dict(), indent=2, sort_keys=True))
        else:
            steps_line = " → ".join(
                f"{s.step_key}({s.assignee})" for s in tpl.steps)
            print(f"Created {tpl.id}  ({tpl.name})\n  {steps_line}")
        return 0

    if action == "list":
        with _kbc.connect_closing() as conn:
            templates = list_templates(conn, board=board)
        if as_json:
            print(json.dumps([t.as_dict() for t in templates], indent=2, sort_keys=True))
            return 0
        if not templates:
            print("(no workflow templates)")
            return 0
        for t in templates:
            steps_line = " → ".join(f"{s.step_key}({s.assignee})" for s in t.steps)
            print(f"{t.id}  {t.name}\n    {steps_line}")
        return 0

    if action == "show":
        try:
            with _kbc.connect_closing() as conn:
                tpl = get_template(conn, board=board, template_id=args.template_id)
        except WorkflowTemplateError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        if tpl is None:
            print(f"kanban: no workflow template {args.template_id!r} on board {board!r}",
                  file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(tpl.as_dict(), indent=2, sort_keys=True))
        else:
            print(f"{tpl.id}  {tpl.name}  (by {tpl.created_by})")
            for i, s in enumerate(tpl.steps, start=1):
                flags = []
                if s.review:
                    flags.append("review")
                if s.approval_type:
                    flags.append(f"approval:{s.approval_type}")
                suffix = f"  [{', '.join(flags)}]" if flags else ""
                print(f"  {i}. {s.step_key} — {s.title} (role {s.assignee}){suffix}")
        return 0

    print(f"kanban: unknown workflow-template action {action!r}", file=sys.stderr)
    return 2


def _acting_profile() -> str:
    try:
        from hermes_cli.profiles import current_profile_name

        return current_profile_name("default")
    except Exception:
        return "cli"
