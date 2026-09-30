"""Kanban approval governance (governance stage 2): generic human-approval engine.

Spec: ``docs/kanban-governance-spec.md`` §4 (Stufe 2). One ``approvals`` table in
the board DB, one kernel module, three surfaces:

* **Kernel** — :func:`request_approval` (idempotent; dedupes an identical OPEN
  request so a worker that lost the tool response can re-request and read the
  same id back), :func:`decide_approval` (approve/reject/revise) and the
  server-side subject fingerprint with drift invalidation. Fingerprints are
  ALWAYS computed here from the live subject — never accepted from the caller.
* **CLI** — :func:`dispatch_approval` for ``hermes kanban approval
  request|list|show|approve|reject|revise`` — the human decision surface.
* **Worker tool** — ``kanban_approval_request`` (request only). Workers can ask,
  never decide: there is no approve tool, and :func:`decide_approval` refuses to
  run inside a dispatched worker context (``HERMES_KANBAN_TASK`` set) or for
  ``approver == requester`` (no self-approval). Fencing is fail-closed.

Drift contract (fail-closed): at every decision AND at every status read the
fingerprint is recomputed from the CURRENT subject. A changed subject
invalidates the open request (``subject_drift``); a subject that can no longer
be resolved (document deleted, budget row removed) invalidates it as
``subject_unresolvable`` — in doubt invalidated, never "still counts". Each
invalidation appends exactly one ``approval_invalidated`` event; a second
decision attempt on the invalidated row is an error, not another event.

An approval is never created without a stable subject: the subject must exist
and be hashable at request time (task row, budget row, readable document,
parseable release plan) — otherwise the request is refused.

Approving releases exactly the tasks whose LATEST ``blocked`` event carries the
exact reason ``approval:<id>`` (binding happens through the existing
``kanban_block(kind='needs_input', reason='approval:<id>')`` flow — no new block
type). The release runs inside the decision transaction via the same unblock
semantics as ``kanban_unblock`` (parent re-gating, resume status, sticky-block
exit) — it is the human decision opening the gate, not an auto-unblock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Tables live in ``kanban_db.SCHEMA_SQL`` (single source of truth); this module
# only references them by name.
APPROVAL_TABLE = "approvals"
APPROVAL_EVENTS_TABLE = "approval_events"

VALID_APPROVAL_TYPES = frozenset({"strategy", "hire", "budget", "action", "release"})
VALID_SUBJECT_KINDS = frozenset({"task", "budget", "document", "release_plan"})
VALID_APPROVAL_STATUSES = frozenset({
    "pending", "approved", "rejected", "revision_requested", "invalidated",
})
# An open request can still be decided; dedup applies while a request is open.
OPEN_APPROVAL_STATUSES = frozenset({"pending", "revision_requested"})
DECISION_TO_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "revise": "revision_requested",
}

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# ``needs_input`` block reasons that bind a task to an approval decision.
_APPROVAL_REASON_RE = re.compile(r"^approval:(ap_[0-9a-f]+)$")

EVENT_REQUESTED = "approval_requested"
EVENT_DECIDED = "approval_decided"
EVENT_INVALIDATED = "approval_invalidated"


class ApprovalStateError(ValueError):
    """The decision cannot be applied (already decided / invalidated / drifted)."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _kanban_approvals_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly() or {}).get("kanban", {})
        return cfg.get("approvals") if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def approvals_enabled() -> bool:
    """``kanban.approvals.enabled`` (default False — the worker tool stays hidden)."""
    try:
        return bool(_kanban_approvals_cfg().get("enabled", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Canonical subject fingerprints (server-side, never caller-supplied)
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    """Deterministic serialization: sorted keys, no whitespace, stable text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_subject(
    conn, *, board: str, subject_kind: str, subject_ref: str,
    period: Optional[str] = None,
) -> tuple[str, str]:
    """``(subject_id, fingerprint)`` for a subject that must exist NOW.

    Raises ``ValueError`` when there is no stable subject to approve — the
    request is then refused ("no approval without a stable subject"). The
    fingerprint is computed here from live state; callers never supply it.
    """
    from hermes_cli import kanban_db as _kb

    kind = str(subject_kind or "").strip()
    ref = str(subject_ref or "").strip()
    if kind not in VALID_SUBJECT_KINDS:
        raise ValueError(
            f"subject_kind must be one of {sorted(VALID_SUBJECT_KINDS)}, got {subject_kind!r}")
    if not ref:
        raise ValueError("subject_ref is required (task id, budget 'scope:ref', document path or release-plan JSON)")

    if kind == "task":
        task = _kb.get_task(conn, ref)
        if task is None:
            raise ValueError(f"no stable subject: task {ref!r} does not exist on this board")
        artifacts = []
        for row in conn.execute(
            "SELECT filename, stored_path FROM task_attachments WHERE task_id = ? ORDER BY filename",
            (ref,),
        ).fetchall():
            try:
                artifact_sha = _sha256_file(row["stored_path"])
            except OSError:
                # The blob is gone: an unstable artifact must change the
                # fingerprint (drift), not crash the fingerprinting.
                artifact_sha = "missing"
            artifacts.append({"name": row["filename"], "sha256": artifact_sha})
        canonical = {
            "kind": "task",
            "task_id": ref,
            "title": task.title,
            "body": task.body or "",
            "completion_contract": task.completion_contract or "",
            "result_sha256": _sha256_text(task.result or ""),
            "artifacts": artifacts,
        }
        return ref, _sha256_text(_canonical_json(canonical))

    if kind == "budget":
        if not period or not _PERIOD_RE.match(str(period)):
            raise ValueError("type 'budget' requires --period 'YYYY-MM' (the month being approved)")
        scope, sep, budget_ref = ref.partition(":")
        if not sep or not budget_ref:
            raise ValueError("budget subject_ref must be '<scope>:<ref>' (e.g. 'profile:alice')")
        # The governing row: an exact-period row wins over the recurring one.
        row = conn.execute(
            "SELECT limit_usd FROM kanban_budgets "
            "WHERE board = ? AND scope = ? AND ref = ? AND period IN (?, 'persist') "
            "ORDER BY CASE period WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (board, scope, budget_ref, str(period), str(period)),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"no stable subject: no budget row for {scope}:{budget_ref} "
                f"on board {board!r} covering {period}")
        canonical = {
            "kind": "budget",
            "board": board,
            "scope": scope,
            "ref": budget_ref,
            "period": str(period),
            "limit_usd": float(row["limit_usd"]),
        }
        return f"{scope}:{budget_ref}", _sha256_text(_canonical_json(canonical))

    if kind == "document":
        try:
            content_sha = _sha256_file(ref)
        except OSError as exc:
            raise ValueError(f"no stable subject: document {ref!r} is not readable ({exc})") from None
        # Content-addressed: the fingerprint tracks the document's CONTENT, so
        # two copies of the same text hash equal and an edit invalidates.
        canonical = {"kind": "document", "sha256": content_sha}
        return ref, _sha256_text(_canonical_json(canonical))

    # kind == "release_plan": the plan JSON is the subject, self-contained and
    # therefore immutable by construction — a CHANGED plan is a NEW request
    # (different fingerprint), it cannot "drift" under an open one.
    try:
        plan = json.loads(ref)
    except json.JSONDecodeError as exc:
        raise ValueError(f"no stable subject: release plan is not valid JSON ({exc})") from None
    if not isinstance(plan, dict):
        raise ValueError("no stable subject: release plan must be a JSON object")
    canonical = {"kind": "release_plan", "plan": plan}
    digest = _sha256_text(_canonical_json(canonical))
    return f"release:{digest}", digest


def _recompute_fingerprint(conn, row) -> Optional[str]:
    """Fingerprint of the CURRENT subject of an approval row; ``None`` when the
    subject can no longer be resolved at all (fail-closed signal)."""
    if row["subject_kind"] == "release_plan":
        # Self-contained subject: recomputing against itself always matches.
        return row["subject_fingerprint"]
    try:
        _, fingerprint = resolve_subject(
            conn, board=row["board"], subject_kind=row["subject_kind"],
            subject_ref=row["subject_id"], period=row["period"],
        )
        return fingerprint
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

_APPROVAL_COLUMNS = (
    "id, board, tenant, type, subject_kind, subject_id, subject_fingerprint, "
    "requester, request_note, status, approver, decided_at, decided_via, "
    "decision_note, invalidation_reason, period, requested_at, updated_at"
)


def _row_dict(row) -> dict[str, Any]:
    return {key: row[key] for key in (
        "id", "board", "tenant", "type", "subject_kind", "subject_id",
        "subject_fingerprint", "requester", "request_note", "status",
        "approver", "decided_at", "decided_via", "decision_note",
        "invalidation_reason", "period", "requested_at", "updated_at",
    )}


def _append_approval_event(conn, approval_id: str, kind: str, payload: Optional[dict]) -> None:
    """Insert one audit event row inside the caller's transaction."""
    conn.execute(
        f"INSERT INTO {APPROVAL_EVENTS_TABLE} (approval_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (approval_id, kind,
         json.dumps(payload, sort_keys=True) if payload is not None else None,
         int(time.time())),
    )


def approval_events(conn, approval_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT id, kind, payload, created_at FROM {APPROVAL_EVENTS_TABLE} "
        "WHERE approval_id = ? ORDER BY id", (approval_id,),
    ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"],
         "payload": json.loads(r["payload"]) if r["payload"] else None,
         "created_at": r["created_at"]}
        for r in rows
    ]


def _invalidate(conn, row, reason: str, *, now: int, observed: Optional[str] = None) -> bool:
    """Fail-closed drift invalidation of ONE open request (exactly one event).

    The ``status IN (open)`` guard is the idempotency fence: a second attempt
    on an already-invalidated row updates nothing and appends NO second event.
    """
    cur = conn.execute(
        f"UPDATE {APPROVAL_TABLE} SET status = 'invalidated', "
        "invalidation_reason = ?, updated_at = ? "
        "WHERE id = ? AND status IN ('pending', 'revision_requested')",
        (reason, int(now), row["id"]),
    )
    if not cur.rowcount:
        return False
    payload: dict[str, Any] = {"reason": reason, "approval_id": row["id"]}
    if observed:
        payload["observed_fingerprint"] = observed
    _append_approval_event(conn, row["id"], EVENT_INVALIDATED, payload)
    return True


def _refresh_drift(conn, row, *, now: Optional[int] = None) -> dict[str, Any]:
    """Re-check one open approval against its current subject (read-side, too).

    Returns the CURRENT row dict — invalidated when the subject drifted or can
    no longer be resolved. Terminal rows are returned untouched (their subject
    was judged when they were decided; re-reading history must not rewrite it).
    """
    if row["status"] in OPEN_APPROVAL_STATUSES:
        current = _recompute_fingerprint(conn, row)
        if current is None:
            _invalidate(conn, row, "subject_unresolvable", now=int(now or time.time()))
        elif current != row["subject_fingerprint"]:
            _invalidate(conn, row, "subject_drift", now=int(now or time.time()), observed=current)
        fresh = conn.execute(
            f"SELECT * FROM {APPROVAL_TABLE} WHERE id = ?", (row["id"],),
        ).fetchone()
        return _row_dict(fresh) if fresh is not None else _row_dict(row)
    return _row_dict(row)


# ---------------------------------------------------------------------------
# Request (kernel; workers and humans both may ask)
# ---------------------------------------------------------------------------

def request_approval(
    conn,
    *,
    board: str,
    type: str,
    subject_kind: str,
    subject_ref: str,
    requester: str,
    note: Optional[str] = None,
    period: Optional[str] = None,
    via: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Create an approval request — idempotently.

    An identical OPEN request (same board/type/subject/requester AND unchanged
    subject fingerprint) returns the existing row with ``deduplicated: True``
    and appends nothing: a caller that lost the first response re-requests and
    reads the same approval id back. The row carries the server-computed
    fingerprint; without a stable subject this raises ``ValueError``.
    """
    from hermes_cli import kanban_db_connect as _kbc

    approval_type = str(type or "").strip()
    if approval_type not in VALID_APPROVAL_TYPES:
        raise ValueError(
            f"approval type must be one of {sorted(VALID_APPROVAL_TYPES)}, got {type!r}")
    if approval_type == "budget" and subject_kind != "budget":
        raise ValueError("approval type 'budget' requires subject_kind 'budget'")
    requester_value = str(requester or "").strip()
    if not requester_value:
        raise ValueError("requester is required (the asking profile)")

    now_value = int(now or time.time())
    tenant = None
    if subject_kind == "task":
        from hermes_cli import kanban_db as _kb

        task = _kb.get_task(conn, str(subject_ref or "").strip())
        if task is not None:
            tenant = task.tenant

    subject_id, fingerprint = resolve_subject(
        conn, board=board, subject_kind=subject_kind,
        subject_ref=subject_ref, period=period,
    )

    with _kbc.write_txn(conn):
        existing = conn.execute(
            f"SELECT * FROM {APPROVAL_TABLE} WHERE board = ? AND type = ? "
            "AND subject_kind = ? AND subject_id = ? AND subject_fingerprint = ? "
            "AND requester = ? AND status IN ('pending', 'revision_requested')",
            (board, approval_type, subject_kind, subject_id, fingerprint, requester_value),
        ).fetchone()
        if existing is not None:
            # Read-back path: also fail-closed on drift before returning.
            fresh = _refresh_drift(conn, existing, now=now_value)
            fresh["deduplicated"] = True
            return fresh
        approval_id = "ap_" + secrets.token_hex(8)
        conn.execute(
            f"INSERT INTO {APPROVAL_TABLE} ({_APPROVAL_COLUMNS}) VALUES ("
            "?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,NULL,NULL,?,?,?)",
            (approval_id, board, tenant, approval_type, subject_kind, subject_id,
             fingerprint, requester_value, note, period, now_value, now_value),
        )
        _append_approval_event(conn, approval_id, EVENT_REQUESTED, {
            "requester": requester_value, "type": approval_type,
            "subject_kind": subject_kind, "subject_id": subject_id,
            "subject_fingerprint": fingerprint, "via": via, "note": note,
        })
        row = conn.execute(
            f"SELECT * FROM {APPROVAL_TABLE} WHERE id = ?", (approval_id,),
        ).fetchone()
        result = _row_dict(row)
        result["deduplicated"] = False
        return result


# ---------------------------------------------------------------------------
# Decision (human surfaces only)
# ---------------------------------------------------------------------------

def _assert_human_decision_context() -> None:
    """Fail-closed worker fence: a dispatched worker process never decides.

    ``HERMES_KANBAN_TASK`` is stamped by the dispatcher into every worker env;
    the CLI invoked from a worker's terminal therefore fails here too. This is
    a fence, not a trust boundary (like the delegate-child guard) — the hard
    boundary is that the worker toolset has no decide tool at all.
    """
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        raise PermissionError(
            "workers cannot decide human approvals: this process is a dispatched "
            "kanban worker (HERMES_KANBAN_TASK is set). Decisions run through the "
            "human surfaces only (CLI `hermes kanban approval decide`, dashboard, "
            "gateway chat).")


def bound_tasks(conn, approval_id: str) -> list[str]:
    """Tasks currently held by ``approval:<id>`` — the exact-reason contract.

    A task counts as bound when it is ``blocked`` AND its LATEST ``blocked``
    event carries the exact reason ``approval:<approval_id>``. A task blocked
    later for a different reason, or since unblocked, is not released by this
    approval (spec: "exakt ``approval:<id>``", Vorher/Nachher-Statusdiff).
    """
    from hermes_cli import kanban_db as _kb

    reason = f"approval:{approval_id}"
    candidates = conn.execute(
        "SELECT DISTINCT task_id FROM task_events "
        "WHERE kind = 'blocked' AND payload LIKE ?",
        (f'%"{reason}"%',),
    ).fetchall()
    bound: list[str] = []
    for row in candidates:
        tid = row["task_id"]
        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        if task is None or task["status"] != "blocked":
            continue
        latest = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        if latest is not None and _kb._json_dict(latest["payload"]).get("reason") == reason:
            bound.append(tid)
    return sorted(bound)


def decide_approval(
    conn,
    approval_id: str,
    *,
    decision: str,
    approver: str,
    note: Optional[str] = None,
    via: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """``approve | reject | revise`` one approval — the human decision.

    Fences (all fail-closed, none mutate state on refusal):
    * dispatched-worker context (``HERMES_KANBAN_TASK``) → ``PermissionError``;
    * ``approver == requester`` → ``PermissionError`` (no self-approval);
    * subject drifted / unresolvable → the open request is invalidated with
      exactly one ``approval_invalidated`` event and the decision is refused
      (``ApprovalStateError``);
    * already decided (approved/rejected) → ``ApprovalStateError``, no event.

    ``approve`` releases exactly the :func:`bound_tasks` inside the same
    transaction, using the shared unblock semantics (parent re-gating, resume
    status) — the human decision opens the gate, no auto-unblock elsewhere.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    if decision not in DECISION_TO_STATUS:
        raise ValueError(
            f"decision must be one of {sorted(DECISION_TO_STATUS)}, got {decision!r}")
    _assert_human_decision_context()
    approver_value = str(approver or "").strip()
    if not approver_value:
        raise ValueError("approver is required (the deciding profile)")
    now_value = int(now or time.time())
    new_status = DECISION_TO_STATUS[decision]

    # Refusals are collected and raised AFTER the transaction commits: the
    # drift invalidation inside MUST survive a refused decision (the row stays
    # 'invalidated' with its exactly-one event) — an exception inside the
    # write_txn would roll the invalidation back with it.
    refusal: Optional[BaseException] = None
    result: dict[str, Any] = {}
    with _kbc.write_txn(conn):
        row = conn.execute(
            f"SELECT * FROM {APPROVAL_TABLE} WHERE id = ?", (approval_id,),
        ).fetchone()
        if row is None or row["board"] != (_kb.get_current_board() or _kb.DEFAULT_BOARD):
            # Board isolation: separate boards live in separate DB files, and a
            # row whose board column does not match the resolved slug is not
            # this board's approval — fail closed as "not found".
            refusal = LookupError(f"approval {approval_id!r} not found on this board")
        else:
            # Drift first (stateful, fail-closed): an invalidated request can
            # no longer be decided — a new request for the changed subject is
            # needed. The invalidation COMMITS even though the decision below
            # is refused.
            if row["status"] in OPEN_APPROVAL_STATUSES:
                current = _recompute_fingerprint(conn, row)
                if current is None:
                    _invalidate(conn, row, "subject_unresolvable", now=now_value)
                elif current != row["subject_fingerprint"]:
                    _invalidate(conn, row, "subject_drift", now=now_value, observed=current)
                row = conn.execute(
                    f"SELECT * FROM {APPROVAL_TABLE} WHERE id = ?", (approval_id,),
                ).fetchone()
            status = row["status"]
            if status == "invalidated":
                refusal = ApprovalStateError(
                    f"approval {approval_id} is invalidated ({row['invalidation_reason']}): "
                    "the subject changed after the request — file a new approval request "
                    "for the current subject")
            elif status not in OPEN_APPROVAL_STATUSES:
                refusal = ApprovalStateError(
                    f"approval {approval_id} is already decided ({status}) — "
                    "decisions apply once; start a new request instead")
            elif approver_value == str(row["requester"]).strip():
                refusal = PermissionError(
                    f"self-approval forbidden: requester and approver are both "
                    f"{approver_value!r}. A human gate must be decided by someone else.")

            if refusal is None:
                decided = conn.execute(
                    f"UPDATE {APPROVAL_TABLE} SET status = ?, approver = ?, decided_at = ?, "
                    "decided_via = ?, decision_note = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (new_status, approver_value, now_value, via, note, now_value,
                     approval_id, status),
                )
                if not decided.rowcount:  # concurrent decision won the race
                    refusal = ApprovalStateError(
                        f"approval {approval_id} changed state concurrently — re-read it")

            if refusal is None:
                released: list[str] = []
                if decision == "approve":
                    for tid in bound_tasks(conn, approval_id):
                        # Same-transaction release with the shared unblock
                        # semantics (parent re-gating, resume status).
                        if _kb._unblock_task_in_txn(conn, tid):
                            released.append(tid)

                _append_approval_event(conn, approval_id, EVENT_DECIDED, {
                    "decision": decision, "status": new_status, "approver": approver_value,
                    "via": via, "note": note, "released_tasks": released,
                })
                fresh = conn.execute(
                    f"SELECT * FROM {APPROVAL_TABLE} WHERE id = ?", (approval_id,),
                ).fetchone()
                result = _row_dict(fresh)
                result["released_tasks"] = released

    if refusal is not None:
        raise refusal
    return result


# ---------------------------------------------------------------------------
# Reads (CLI / dashboard; drift-refreshing)
# ---------------------------------------------------------------------------

def get_approval(conn, approval_id: str, *, refresh: bool = True) -> dict[str, Any]:
    """One approval row; open rows are drift-checked first (fail-closed read)."""
    row = conn.execute(
        f"SELECT * FROM {APPROVAL_TABLE} WHERE id = ?", (approval_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"approval {approval_id!r} not found on this board")
    if refresh and row["status"] in OPEN_APPROVAL_STATUSES:
        from hermes_cli import kanban_db_connect as _kbc

        with _kbc.write_txn(conn):
            return _refresh_drift(conn, row)
    return _row_dict(row)


def list_approvals(
    conn, board: str, *, status: Optional[str] = None, type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """All approvals of one board, newest first; open rows drift-checked.

    Board is a hard boundary: rows of other boards are never visible here.
    """
    if status is not None and status not in VALID_APPROVAL_STATUSES:
        raise ValueError(
            f"status filter must be one of {sorted(VALID_APPROVAL_STATUSES)}, got {status!r}")
    if type is not None and type not in VALID_APPROVAL_TYPES:
        raise ValueError(
            f"type filter must be one of {sorted(VALID_APPROVAL_TYPES)}, got {type!r}")
    sql = f"SELECT * FROM {APPROVAL_TABLE} WHERE board = ?"
    params: list[Any] = [board]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if type:
        sql += " AND type = ?"
        params.append(type)
    sql += " ORDER BY requested_at DESC, id"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    from hermes_cli import kanban_db_connect as _kbc

    with _kbc.write_txn(conn):
        return [_refresh_drift(conn, row) for row in rows]


def approval_hint_for_task(conn, task_id: str) -> Optional[dict[str, Any]]:
    """Approval info for a task held by ``approval:<id>`` (``kanban_show`` hint).

    ``None`` when the task is not blocked by an approval. The hint refreshes
    drift first so the reader never sees a stale "pending" for a subject that
    no longer matches.
    """
    from hermes_cli import kanban_db as _kb

    task = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if task is None or task["status"] != "blocked":
        return None
    latest = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if latest is None:
        return None
    match = _APPROVAL_REASON_RE.match(str(_kb._json_dict(latest["payload"]).get("reason") or ""))
    if not match:
        return None
    approval_id = match.group(1)
    try:
        row = get_approval(conn, approval_id, refresh=True)
    except LookupError:
        return {"id": approval_id, "status": "unknown"}
    return {"id": approval_id, "status": row["status"], "type": row["type"]}


# ---------------------------------------------------------------------------
# CLI surface: ``hermes kanban approval request|list|show|approve|reject|revise``
# ---------------------------------------------------------------------------

def _acting_profile() -> str:
    try:
        from hermes_cli.profiles import current_profile_name

        return current_profile_name("user") or "user"
    except Exception:
        return "user"


def _cli_err(message: str) -> int:
    print(f"kanban: {message}")
    return 1


def _resolve_board_slug(board: Optional[str]) -> str:
    from hermes_cli import kanban_db as _kb

    return board or _kb.get_current_board() or _kb.DEFAULT_BOARD


def dispatch_approval(args: argparse.Namespace) -> int:
    """``hermes kanban approval …`` — the human approval surface.

    Workers never decide through here: :func:`decide_approval` refuses worker
    contexts, and the worker toolset exposes only ``kanban_approval_request``.
    """
    from hermes_cli import kanban_db_connect as _kbc

    action = getattr(args, "approval_action", None) or "list"
    board = _resolve_board_slug(None)
    as_json = bool(getattr(args, "json", False))

    if action == "request":
        try:
            with _kbc.connect_closing() as conn:
                result = request_approval(
                    conn,
                    board=board,
                    type=args.type,
                    subject_kind=args.subject_kind,
                    subject_ref=args.subject_ref,
                    requester=_acting_profile(),
                    note=getattr(args, "note", None),
                    period=getattr(args, "period", None),
                    via="cli",
                )
        except ValueError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        dedup = " (deduplicated: an identical open request already existed)" if result["deduplicated"] else ""
        print(
            f"approval request: {result['id']} status={result['status']} "
            f"type={result['type']} subject={result['subject_kind']}:{result['subject_id']}{dedup}"
        )
        print(
            "decide via: hermes kanban approval approve|reject|revise "
            f"{result['id']} [--note ...]"
        )
        return 0

    if action == "list":
        try:
            with _kbc.connect_closing() as conn:
                rows = list_approvals(
                    conn, board,
                    status=getattr(args, "status", None),
                    type=getattr(args, "type", None),
                )
        except ValueError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print(f"No approvals on board {board} (hermes kanban approval request …)")
            return 0
        print(f"Board: {board}   ({len(rows)} approval(s))")
        print(f"{'id':21s} {'type':9s} {'subject':32s} {'status':18s} requester  approver")
        for row in rows:
            subject = f"{row['subject_kind']}:{row['subject_id']}"
            if len(subject) > 32:
                subject = subject[:29] + "..."
            print(
                f"{row['id']:21s} {row['type']:9s} {subject:32s} {row['status']:18s} "
                f"{row['requester']:10s} {row['approver'] or '-'}"
            )
        return 0

    if action == "show":
        try:
            with _kbc.connect_closing() as conn:
                row = get_approval(conn, args.approval_id)
                events = approval_events(conn, args.approval_id)
        except LookupError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps({"approval": row, "events": events}, indent=2, sort_keys=True))
            return 0
        print(f"Approval: {row['id']}   Board: {row['board']}   Status: {row['status']}")
        print(
            f"type={row['type']} subject={row['subject_kind']}:{row['subject_id']} "
            f"period={row['period'] or '-'}"
        )
        print(f"requester={row['requester']} requested_at={row['requested_at']}")
        if row["request_note"]:
            print(f"request note: {row['request_note']}")
        print(f"fingerprint: {row['subject_fingerprint']}")
        if row["status"] == "invalidated":
            print(f"invalidation_reason: {row['invalidation_reason']}")
        if row["decided_at"]:
            print(
                f"decided_at={row['decided_at']} approver={row['approver']} "
                f"via={row['decided_via']} note={row['decision_note'] or '-'}"
            )
        print("events:")
        for event in events:
            print(f"  [{event['created_at']}] {event['kind']} {json.dumps(event['payload'], sort_keys=True) if event['payload'] else ''}")
        return 0

    if action in ("approve", "reject", "revise"):
        try:
            with _kbc.connect_closing() as conn:
                result = decide_approval(
                    conn, args.approval_id,
                    decision={"approve": "approve", "reject": "reject", "revise": "revise"}[action],
                    approver=_acting_profile(),
                    note=getattr(args, "note", None),
                    via="cli",
                )
        except LookupError as exc:
            return _cli_err(str(exc))
        except ApprovalStateError as exc:
            return _cli_err(str(exc))
        except PermissionError as exc:
            print(f"kanban: {exc}")
            return 2
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        print(f"approval {result['id']}: {action} -> {result['status']} by {result['approver']}")
        if result.get("released_tasks"):
            print(f"released task(s): {', '.join(result['released_tasks'])}")
        return 0

    return _cli_err(f"unknown approval action {action!r}")
