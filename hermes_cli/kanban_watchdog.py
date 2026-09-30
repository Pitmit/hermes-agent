"""Kanban task-bound independent watchdog (governance stage 5).

Spec: ``docs/kanban-governance-spec.md`` §7 (Stufe 5 — "unabhängiger
nicht-reparierender Reviewer/Watchdog"). One ``task_watchdogs`` row per task
(ONE active watchdog per task) names an INDEPENDENT reviewer profile plus
reviewer-facing instructions. When the watched task enters a STOPPED state
(``review`` handoff or ``blocked``), the kernel fingerprints that stopped
state server-side and fires the reviewer exactly once per DISTINCT
fingerprint (``task_watchdog_firings`` is the exactly-once ledger).

Vocabulary contract — the watchdog NEVER repairs. Its decision verbs are
exactly ``accept | request_changes | reopen | reassign``; anything else is
refused. The verbs map onto the existing kernel transitions:

* ``accept`` — record the verdict; the task is NOT mutated (the watchdog
  never completes or edits work; the normal review lane still owns the
  review decision itself).
* ``request_changes`` — review stops only: the task routes back to the
  implementer (parent re-gating, claim cleared, ``changes_requested``
  event — the same routing the claimed-reviewer path uses).
* ``reopen`` — review stops: the ``review_reopened`` semantics; ``transient``
  blocks: the shared unblock semantics. Human gates (``needs_input`` /
``capability``) are NEVER reopened here — those open only by an explicit
human unblock or an approval decision (spec §12-6).
* ``reassign`` — assignee handoff with the standard ``assigned`` event.

Round limit — a stop with an ALREADY-FIRED fingerprint is an identical
round (the subject did not change). The first stop fires; identical rounds
only count up. At ``WATCHDOG_ROUND_LIMIT`` (3, the same shape as
BLOCK_RECURRENCE_LIMIT) the watchdog escalates ONCE, stickily: the task
routes to ``triage`` (the established recurrence-routing pattern) with a
``watchdog_escalated`` event and the watchdog row gets ``escalated_at``.
While escalated the watchdog stays silent — only a human removes it; no
tick ever re-fires or auto-resolves it.

Fences (fail-closed, none mutate on refusal):

* no self-review — the reviewer may not be the task's own assignee or the
  implementer of the current handoff (checked at CREATE and again at every
  decision);
* the worker tool decides only as the watchdog's OWN reviewer profile;
* human gates stay human (``reopen`` refuses ``needs_input``/``capability``);
* one decision per firing (exactly-once; the outcome is fingerprinted and
  a replay is refused, not silently re-applied).

The dispatcher integration is flag-gated (``kanban.watchdog.tick_enabled``,
default off — flags-off ticks stay byte-identical) and fail-open; the worker
tools are gated on ``kanban.watchdog.enabled``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Tables live in ``kanban_db.SCHEMA_SQL`` (single source of truth); this
# module only references them by name.
WATCHDOG_TABLE = "task_watchdogs"
FIRINGS_TABLE = "task_watchdog_firings"

#: Identical-round limit. The first stop with a fingerprint FIRES; identical
#: re-stops only count. When the count reaches this limit the watchdog
#: escalates stickily (triage routing + one ``watchdog_escalated`` event).
#: Same shape as ``kanban_db.BLOCK_RECURRENCE_LIMIT``.
WATCHDOG_ROUND_LIMIT = 3

#: The complete decision vocabulary. The watchdog never repairs — there is
#: deliberately no fix/complete/unblock-human-gates verb.
WATCHDOG_VERBS = frozenset({"accept", "request_changes", "reopen", "reassign"})

VERB_TO_OUTCOME = {
    "accept": "accepted",
    "request_changes": "changes_requested",
    "reopen": "reopened",
    "reassign": "reassigned",
}

VALID_WATCHDOG_STATUSES = frozenset({"active", "retired"})

#: ``task_events`` kinds emitted by this module (audit trail; one event per
#: transition, exactly-once guards on the DB rows they describe).
EVENT_CREATED = "watchdog_created"
EVENT_REMOVED = "watchdog_removed"
EVENT_FIRED = "watchdog_fired"
EVENT_ROUND = "watchdog_round"
EVENT_ESCALATED = "watchdog_escalated"
EVENT_DECIDED = "watchdog_decided"

#: The stop event kinds that mark a watched stop (a ``review`` handoff or a
#: typed block). ``dependency_wait`` parks in ``todo`` and is not a stop.
STOP_EVENT_KINDS = ("review_requested", "blocked")

#: Block kinds the watchdog may reopen: only the dispatcher-retry class.
#: ``needs_input``/``capability`` are HUMAN gates (spec §12-6) — the watchdog
#: never opens them.
REOPENABLE_BLOCK_KINDS = frozenset({"transient"})


class WatchdogStateError(ValueError):
    """The decision/check cannot be applied (already decided / wrong state)."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _kanban_watchdog_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly() or {}).get("kanban", {})
        return cfg.get("watchdog") if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def watchdog_enabled() -> bool:
    """``kanban.watchdog.enabled`` (default False — worker tools stay hidden)."""
    try:
        return bool(_kanban_watchdog_cfg().get("enabled", False))
    except Exception:
        return False


def watchdog_tick_enabled() -> bool:
    """``kanban.watchdog.tick_enabled`` (default False — dispatcher-neutral)."""
    try:
        return bool(_kanban_watchdog_cfg().get("tick_enabled", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Server-side fingerprints
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_implementer(conn, task_row) -> Optional[str]:
    """The profile that did the work of the current handoff — the same
    provenance ``request_review`` records (current run's profile, else the
    assignee when it differs from a reviewer)."""
    from hermes_cli import kanban_db as _kb

    implementer = None
    if task_row.current_run_id is not None:
        arow = conn.execute(
            "SELECT profile FROM task_runs WHERE id = ?", (task_row.current_run_id,),
        ).fetchone()
        implementer = arow["profile"] if arow else None
    if implementer is None:
        implementer = task_row.assignee
    return implementer if isinstance(implementer, str) and implementer.strip() else None


def stopped_state_fingerprint(
    conn, task, *, board: str,
) -> tuple[str, str, int]:
    """``(stop_kind, fingerprint, stop_event_id)`` of a STOPPED task.

    The fingerprint covers the review/blocker subject AND the stop cause:
    the proven task-subject fingerprint (same canonical shape as the
    approvals engine: title, body, contract, result, artifacts) combined
    with the stop specifics (review summary / block kind + reason). A
    changed subject, changed artifacts or a different stop cause is a NEW
    fingerprint and may fire again; an unchanged bounce is an identical
    round. ``stop_event_id`` is the id of the newest stop event (0 when the
    stopped state has no stop event on record — then rounds can only ever
    count once for that fingerprint, which fails safe, never double).

    Raises ``ValueError`` when the task is not in a watched stopped state.
    """
    from hermes_cli import kanban_db as _kb

    task_id = task.id
    status = task.status
    if status not in ("review", "blocked"):
        raise ValueError(f"task {task_id} is not stopped (status {status!r})")

    # Subject part: reuse the approvals engine's proven task-subject
    # fingerprint (title/body/contract/result/artifacts, server-side).
    _, subject_fp = _kb_resolve_task_subject(conn, board=board, task_id=task_id)

    if status == "review":
        stop_event = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        payload = _kb._json_dict(_kb._row_get(stop_event, "payload"))
        stop = {
            "kind": "review",
            "review_summary": payload.get("summary"),
        }
    else:
        stop_event = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        payload = _kb._json_dict(_kb._row_get(stop_event, "payload"))
        stop = {
            "kind": "blocked",
            "block_kind": task.block_kind,
            "block_reason": payload.get("reason"),
        }
    stop_event_id = int(_kb._row_get(stop_event, "id", 0) or 0)
    fingerprint = _sha256_text(_canonical_json({"subject": subject_fp, "stop": stop}))
    return stop["kind"], fingerprint, stop_event_id


def _kb_resolve_task_subject(conn, *, board: str, task_id: str) -> tuple[str, str]:
    """(subject_id, subject_fingerprint) via the approvals engine's task
    resolver — one subject semantics across both governance stages."""
    from hermes_cli import kanban_approvals as _ka

    return _ka.resolve_subject(conn, board=board, subject_kind="task", subject_ref=task_id)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

_WATCHDOG_COLUMNS = (
    "id, board, tenant, task_id, reviewer, instructions, created_by, "
    "created_at, updated_at, status, escalated_at"
)
_FIRING_COLUMNS = (
    "id, watchdog_id, task_id, board, fingerprint, stop_kind, stop_event_id, "
    "rounds, fired_at, outcome, outcome_fingerprint, decided_by, decided_at, "
    "decided_via, decision_note, updated_at"
)


def _watchdog_dict(row) -> dict[str, Any]:
    return {key: row[key] for key in (
        "id", "board", "tenant", "task_id", "reviewer", "instructions",
        "created_by", "created_at", "updated_at", "status", "escalated_at",
    )}


def _firing_dict(row) -> dict[str, Any]:
    return {key: row[key] for key in (
        "id", "watchdog_id", "task_id", "board", "fingerprint", "stop_kind",
        "stop_event_id", "rounds", "fired_at", "outcome", "outcome_fingerprint",
        "decided_by", "decided_at", "decided_via", "decision_note", "updated_at",
    )}


def get_watchdog(conn, watchdog_id: str) -> dict[str, Any]:
    """One watchdog row (any status — history is auditable)."""
    row = conn.execute(
        f"SELECT * FROM {WATCHDOG_TABLE} WHERE id = ?", (watchdog_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"watchdog {watchdog_id!r} not found on this board")
    return _watchdog_dict(row)


def watchdog_for_task(conn, board: str, task_id: str) -> Optional[dict[str, Any]]:
    """The task's ACTIVE watchdog, or ``None``."""
    row = conn.execute(
        f"SELECT * FROM {WATCHDOG_TABLE} WHERE board = ? AND task_id = ? AND status = 'active'",
        (board, task_id),
    ).fetchone()
    return _watchdog_dict(row) if row is not None else None


def list_watchdogs(conn, board: str, *, status: Optional[str] = None) -> list[dict[str, Any]]:
    """All watchdogs of one board, newest first. Board is a hard boundary."""
    if status is not None and status not in VALID_WATCHDOG_STATUSES:
        raise ValueError(
            f"status filter must be one of {sorted(VALID_WATCHDOG_STATUSES)}, got {status!r}")
    sql = f"SELECT * FROM {WATCHDOG_TABLE} WHERE board = ?"
    params: list[Any] = [board]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id"
    return [_watchdog_dict(r) for r in conn.execute(sql, params).fetchall()]


def list_firings(
    conn, board: str, *, task_id: Optional[str] = None,
    pending: bool = False, watchdog_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Firing ledger rows of one board (optionally one task / one watchdog /
    only undecided)."""
    sql = f"SELECT * FROM {FIRINGS_TABLE} WHERE board = ?"
    params: list[Any] = [board]
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    if watchdog_id:
        sql += " AND watchdog_id = ?"
        params.append(watchdog_id)
    if pending:
        sql += " AND outcome IS NULL"
    sql += " ORDER BY id DESC"
    return [_firing_dict(r) for r in conn.execute(sql, params).fetchall()]


def watchdog_hint_for_task(conn, board: str, task_id: str) -> Optional[dict[str, Any]]:
    """Watchdog info for ``kanban show`` — active watchdog plus its latest
    firing state, or ``None`` when the task is unwatched."""
    wd = watchdog_for_task(conn, board, task_id)
    if wd is None:
        return None
    hint: dict[str, Any] = {
        "id": wd["id"], "reviewer": wd["reviewer"], "escalated": wd["escalated_at"] is not None,
    }
    firings = list_firings(conn, board, task_id=task_id, watchdog_id=wd["id"])
    if firings:
        latest = firings[0]
        hint["latest_firing"] = {
            "fingerprint": latest["fingerprint"], "rounds": latest["rounds"],
            "outcome": latest["outcome"],
        }
    return hint


# ---------------------------------------------------------------------------
# Create / remove
# ---------------------------------------------------------------------------

def create_watchdog(
    conn, *, board: str, task_id: str, reviewer: str,
    instructions: Optional[str] = None, created_by: str,
    via: str = "cli", now: Optional[int] = None,
) -> dict[str, Any]:
    """Attach ONE active independent watchdog to a task.

    Fences (fail-closed, nothing written on refusal):

    * the task must exist on this board;
    * no self-review — ``reviewer`` may not be the task's current assignee
      nor the implementer of the current handoff;
    * one ACTIVE watchdog per task — attach after removing the old one.

    The watchdog never repairs: it only fires the reviewer and later applies
    one of the WATCHDOG_VERBS decisions; that vocabulary is enforced in
    :func:`decide_firing`, not here (this row is a monitoring contract).
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    reviewer_value = _kb._canonical_assignee(str(reviewer or "").strip())
    if not reviewer_value:
        raise ValueError("reviewer is required (the independent reviewer profile)")
    if not str(created_by or "").strip():
        raise ValueError("created_by is required (the attaching profile)")
    now_value = int(now or time.time())

    with _kbc.write_txn(conn):
        task = _kb.get_task(conn, str(task_id or "").strip())
        if task is None:
            raise ValueError(f"task {task_id!r} does not exist on this board")
        existing = watchdog_for_task(conn, board, task.id)
        if existing is not None:
            raise ValueError(
                f"task {task.id} already has an active watchdog ({existing['id']}); "
                "remove it first (hermes kanban watchdog rm)")
        assignee = task.assignee
        implementer = _current_implementer(conn, task)
        if reviewer_value in {assignee, implementer}:
            who = "assignee" if reviewer_value == assignee else "implementer"
            raise PermissionError(
                f"no self-review: reviewer {reviewer_value!r} is the task's {who}. "
                "The watchdog must be an INDEPENDENT reviewer profile.")
        watchdog_id = "wd_" + secrets.token_hex(8)
        conn.execute(
            f"INSERT INTO {WATCHDOG_TABLE} ({_WATCHDOG_COLUMNS}) VALUES ("
            "?,?,?,?,?,?,?,?,?,'active',NULL)",
            (watchdog_id, board, task.tenant, task.id, reviewer_value, instructions,
             str(created_by).strip(), now_value, now_value),
        )
        _kb._append_event(conn, task.id, EVENT_CREATED, {
            "watchdog_id": watchdog_id, "reviewer": reviewer_value,
            "instructions": instructions, "created_by": str(created_by).strip(),
            "via": via,
        })
        row = conn.execute(
            f"SELECT * FROM {WATCHDOG_TABLE} WHERE id = ?", (watchdog_id,),
        ).fetchone()
        return _watchdog_dict(row)


def remove_watchdog(
    conn, watchdog_id: str, *, removed_by: str, via: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Retire a watchdog (audit-preserving: the row stays, status -> retired)."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    if not str(removed_by or "").strip():
        raise ValueError("removed_by is required (the removing profile)")
    now_value = int(now or time.time())
    with _kbc.write_txn(conn):
        row = conn.execute(
            f"SELECT * FROM {WATCHDOG_TABLE} WHERE id = ?", (watchdog_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"watchdog {watchdog_id!r} not found on this board")
        if row["status"] != "retired":
            conn.execute(
                f"UPDATE {WATCHDOG_TABLE} SET status = 'retired', updated_at = ? WHERE id = ?",
                (now_value, watchdog_id),
            )
            _kb._append_event(conn, row["task_id"], EVENT_REMOVED, {
                "watchdog_id": watchdog_id, "removed_by": str(removed_by).strip(), "via": via,
            })
        fresh = conn.execute(
            f"SELECT * FROM {WATCHDOG_TABLE} WHERE id = ?", (watchdog_id,),
        ).fetchone()
        return _watchdog_dict(fresh)


# ---------------------------------------------------------------------------
# Check phase (dispatcher tick + explicit CLI check)
# ---------------------------------------------------------------------------

def check_watchdogs(
    conn, board: str, *, task_id: Optional[str] = None, now: Optional[int] = None,
) -> dict[str, Any]:
    """Check every active watchdog of the board (or one task's).

    For each watched task in a stopped state (``review``/``blocked``):

    * a fingerprint never seen for this watchdog FIRES exactly once — one
      firing row (UNIQUE(watchdog_id, fingerprint)) + one ``watchdog_fired``
      event carrying the reviewer + instructions (the trigger for the
      independent review);
    * a stop with an ALREADY-FIRED fingerprint is an identical round: it
      only counts (one ``watchdog_round`` event per counted round, deduped
      by the stop event id so re-checking one stop never double-counts);
    * at ``WATCHDOG_ROUND_LIMIT`` identical rounds the watchdog escalates
      ONCE, stickily: the task routes to ``triage`` (the recurrence-routing
      pattern) with one ``watchdog_escalated`` event and the watchdog row is
      marked ``escalated_at``; while escalated the watchdog stays silent
      (only a human removes it).

    Escalated, retired watchdogs and unwatched tasks are skipped — old
    cards are never touched by this phase.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    now_value = int(now or time.time())
    report: dict[str, Any] = {"checked": 0, "fired": [], "rounds": [], "escalated": [], "skipped": []}
    sql = f"SELECT * FROM {WATCHDOG_TABLE} WHERE board = ? AND status = 'active' AND escalated_at IS NULL"
    params: list[Any] = [board]
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    rows = conn.execute(sql + " ORDER BY created_at, id", params).fetchall()

    for wd in rows:
        report["checked"] += 1
        task = _kb.get_task(conn, wd["task_id"])
        if task is None:
            report["skipped"].append({"watchdog_id": wd["id"], "task_id": wd["task_id"],
                                      "reason": "task_missing"})
            continue
        if task.status not in ("review", "blocked"):
            continue  # not stopped — nothing to do, no event
        try:
            stop_kind, fingerprint, stop_event_id = stopped_state_fingerprint(
                conn, task, board=board)
        except ValueError:
            continue
        with _kbc.write_txn(conn):
            # Re-read inside the txn: the stop may have moved on.
            task_now = _kb.get_task(conn, wd["task_id"])
            if task_now is None or task_now.status not in ("review", "blocked"):
                continue
            firing = conn.execute(
                f"SELECT * FROM {FIRINGS_TABLE} WHERE watchdog_id = ? AND fingerprint = ?",
                (wd["id"], fingerprint),
            ).fetchone()
            if firing is None:
                # Exactly-once per DISTINCT fingerprint: the INSERT wins the
                # UNIQUE(watchdog_id, fingerprint) race; a loser is a no-op.
                try:
                    conn.execute(
                        f"INSERT INTO {FIRINGS_TABLE} (watchdog_id, task_id, board, "
                        "fingerprint, stop_kind, stop_event_id, rounds, fired_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,1,?,?)",
                        (wd["id"], wd["task_id"], board, fingerprint, stop_kind,
                         int(stop_event_id), now_value, now_value),
                    )
                except Exception:
                    logger.debug("watchdog firing race lost for %s (exactly-once holds)", wd["id"])
                    continue
                _kb._append_event(conn, wd["task_id"], EVENT_FIRED, {
                    "watchdog_id": wd["id"], "fingerprint": fingerprint,
                    "stop_kind": stop_kind, "round": 1,
                    "reviewer": wd["reviewer"], "instructions": wd["instructions"],
                })
                report["fired"].append({"watchdog_id": wd["id"], "task_id": wd["task_id"],
                                        "fingerprint": fingerprint, "stop_kind": stop_kind})
                continue

            # Already fired for this fingerprint: an identical round only
            # counts when the task RE-ENTERED the stopped state (a stop
            # event newer than the last counted one).
            if int(stop_event_id) > int(firing["stop_event_id"]):
                rounds = int(firing["rounds"]) + 1
                cur = conn.execute(
                    f"UPDATE {FIRINGS_TABLE} SET rounds = ?, stop_event_id = ?, "
                    "updated_at = ? WHERE id = ? AND stop_event_id = ?",
                    (rounds, int(stop_event_id), now_value, firing["id"], int(firing["stop_event_id"])),
                )
                if cur.rowcount:
                    _kb._append_event(conn, wd["task_id"], EVENT_ROUND, {
                        "watchdog_id": wd["id"], "fingerprint": fingerprint,
                        "round": rounds, "limit": WATCHDOG_ROUND_LIMIT,
                    })
                    report["rounds"].append({"watchdog_id": wd["id"], "task_id": wd["task_id"],
                                             "round": rounds})
                    if rounds >= WATCHDOG_ROUND_LIMIT:
                        _escalate_sticky(conn, wd, task_now, fingerprint, rounds, now=now_value)
                        report["escalated"].append({
                            "watchdog_id": wd["id"], "task_id": wd["task_id"],
                            "rounds": rounds, "fingerprint": fingerprint,
                        })
    return report


def _escalate_sticky(conn, wd, task, fingerprint: str, rounds: int, *, now: int) -> None:
    """Sticky human escalation, exactly once: route the stopped task to
    ``triage`` (the established recurrence-routing pattern) and freeze the
    watchdog. The ``escalated_at IS NULL`` guard is the exactly-once fence;
    the status guard makes a concurrent lifecycle win race-free."""
    from hermes_cli import kanban_db as _kb

    marked = conn.execute(
        f"UPDATE {WATCHDOG_TABLE} SET escalated_at = ?, updated_at = ? "
        "WHERE id = ? AND escalated_at IS NULL",
        (int(now), int(now), wd["id"]),
    )
    if not marked.rowcount:
        return  # a concurrent check already escalated — exactly-once holds
    prior_status = task.status
    conn.execute(
        "UPDATE tasks SET status = 'triage', claim_lock = NULL, claim_expires = NULL, "
        "worker_pid = NULL, worker_started_at = NULL "
        "WHERE id = ? AND status IN ('review', 'blocked')",
        (wd["task_id"],),
    )
    _kb._append_event(conn, wd["task_id"], EVENT_ESCALATED, {
        "watchdog_id": wd["id"], "rounds": int(rounds), "limit": WATCHDOG_ROUND_LIMIT,
        "fingerprint": fingerprint, "prior_status": prior_status,
        "action": "sticky human escalation: task routed to triage; the watchdog is "
                  "frozen until a human removes it",
    })


# ---------------------------------------------------------------------------
# Decision (apply path — CLI human surface + reviewer worker tool)
# ---------------------------------------------------------------------------

def decide_firing(
    conn, watchdog_id: str, *, verb: str, decider: str,
    note: Optional[str] = None, assignee: Optional[str] = None,
    via: str = "cli", now: Optional[int] = None,
) -> dict[str, Any]:
    """Apply the independent reviewer's verdict on the LATEST open firing.

    The vocabulary is closed (:data:`WATCHDOG_VERBS`) — the watchdog never
    repairs, never completes work, never opens a human gate. Fences (all
    fail-closed, nothing mutated on refusal):

    * unknown verb -> ``ValueError`` (there is deliberately no fix verb);
    * no self-review — ``decider`` may not be the task's current assignee
      or the current handoff's implementer;
    * worker context (``via='tool'``): only the watchdog's OWN reviewer
      profile decides;
    * the firing must be open (``outcome IS NULL``) — one decision per
      firing, exactly-once; the replay is refused, not re-applied.

    Every decision appends exactly one ``watchdog_decided`` audit event and
    stores the server-computed outcome fingerprint.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    verb_value = str(verb or "").strip()
    if verb_value not in WATCHDOG_VERBS:
        raise ValueError(
            f"unknown watchdog verb {verb_value!r}: the watchdog never repairs — "
            f"allowed verbs are {sorted(WATCHDOG_VERBS)}")
    decider_value = str(decider or "").strip()
    if not decider_value:
        raise ValueError("decider is required (the deciding profile)")
    now_value = int(now or time.time())

    refusal: Optional[BaseException] = None
    result: dict[str, Any] = {}
    with _kbc.write_txn(conn):
        wd = conn.execute(
            f"SELECT * FROM {WATCHDOG_TABLE} WHERE id = ?", (watchdog_id,),
        ).fetchone()
        if wd is None:
            raise LookupError(f"watchdog {watchdog_id!r} not found on this board")
        firing = conn.execute(
            f"SELECT * FROM {FIRINGS_TABLE} WHERE watchdog_id = ? AND outcome IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (watchdog_id,),
        ).fetchone()
        if firing is None:
            open_rows = conn.execute(
                f"SELECT COUNT(*) AS n FROM {FIRINGS_TABLE} WHERE watchdog_id = ?",
                (watchdog_id,),
            ).fetchone()
            if not open_rows["n"]:
                raise WatchdogStateError(f"watchdog {watchdog_id} has no firing to decide")
            raise WatchdogStateError(
                f"the latest firing of watchdog {watchdog_id} is already decided "
                f"({firing_outcome_of(conn, watchdog_id)}) — decisions apply exactly once")
        task = _kb.get_task(conn, wd["task_id"])
        if task is None:
            raise LookupError(f"watchdog task {wd['task_id']!r} no longer exists")
        assignee_now = task.assignee
        implementer_now = _current_implementer(conn, task)
        if decider_value in {assignee_now, implementer_now}:
            who = "assignee" if decider_value == assignee_now else "implementer"
            refusal = PermissionError(
                f"no self-review: decider {decider_value!r} is the task's {who}. "
                "The watchdog verdict must come from an independent profile.")
        elif via == "tool" and decider_value != str(wd["reviewer"]).strip():
            refusal = PermissionError(
                f"worker decisions are reserved for the watchdog's reviewer "
                f"{wd['reviewer']!r}; {decider_value!r} may decide only via the "
                "human CLI surface")

        if refusal is None:
            effects = _apply_verb(
                conn, wd, firing, task, verb=verb_value, note=note,
                assignee=assignee, decider=decider_value, now=now_value,
            )
            outcome = VERB_TO_OUTCOME[verb_value]
            outcome_fp = _sha256_text(_canonical_json({
                "watchdog_id": wd["id"], "firing_id": int(firing["id"]),
                "verb": verb_value, "outcome": outcome,
                "note_sha256": _sha256_text(note or ""),
                "assignee": effects.get("assignee"),
            }))
            decided = conn.execute(
                f"UPDATE {FIRINGS_TABLE} SET outcome = ?, outcome_fingerprint = ?, "
                "decided_by = ?, decided_at = ?, decided_via = ?, decision_note = ?, "
                "updated_at = ? WHERE id = ? AND outcome IS NULL",
                (outcome, outcome_fp, decider_value, now_value, via, note, now_value,
                 firing["id"]),
            )
            if not decided.rowcount:
                refusal = WatchdogStateError(
                    f"firing {firing['id']} was decided concurrently — re-read it")
            else:
                _kb._append_event(conn, wd["task_id"], EVENT_DECIDED, {
                    "watchdog_id": wd["id"], "firing_id": int(firing["id"]),
                    "verb": verb_value, "outcome": outcome,
                    "decided_by": decider_value, "via": via, "note": note,
                    "outcome_fingerprint": outcome_fp, **effects,
                })
                fresh = conn.execute(
                    f"SELECT * FROM {FIRINGS_TABLE} WHERE id = ?", (firing["id"],),
                ).fetchone()
                result = {"watchdog": _watchdog_dict(wd), "firing": _firing_dict(fresh)}

    if refusal is not None:
        raise refusal
    return result


def firing_outcome_of(conn, watchdog_id: str) -> Optional[str]:
    """Outcome of the watchdog's latest firing (None = open)."""
    row = conn.execute(
        f"SELECT outcome FROM {FIRINGS_TABLE} WHERE watchdog_id = ? ORDER BY id DESC LIMIT 1",
        (watchdog_id,),
    ).fetchone()
    return row["outcome"] if row is not None else None


def _apply_verb(
    conn, wd, firing, task, *, verb: str, note: Optional[str],
    assignee: Optional[str], decider: str, now: int,
) -> dict[str, Any]:
    """Task-side effect of one allowed verb, inside the caller's txn.

    Mirrors the compositional kernel helpers (the ``_unblock_task_in_txn``
    pattern): no own transaction, no new semantics — each verb maps onto an
    EXISTING transition shape. Raises ``WatchdogStateError`` when the verb
    does not apply to the current stop; nothing was mutated then.
    """
    from hermes_cli import kanban_db as _kb

    task_id = wd["task_id"]
    status = task.status

    if verb == "accept":
        # The watchdog accepts the stopped state — governance acknowledgment
        # only. No mutation: the normal lanes (review dispatch, human
        # unblock) still own every transition.
        return {}

    if verb == "request_changes":
        if status != "review":
            raise WatchdogStateError(
                f"request_changes applies to review stops only (task is {status!r})")
        requested = _kb._latest_event(conn, task_id, "review_requested")
        implementer = None
        if requested is not None:
            implementer = _kb._nonblank_str(
                _kb._json_dict(_kb._row_get(requested, "payload")).get("implementer"))
        if implementer is None:
            implementer = _current_implementer(conn, task)
        _kb._reclaim_dangling_run(conn, task_id, statuses=("review",), now=now,
                                   note="watchdog request_changes")
        new_status = _kb._landing_status_after_parents(conn, task_id)
        params: tuple[Any, ...] = (new_status, *((implementer,) if implementer else ()), task_id)
        cur = conn.execute(
            # consecutive_failures deliberately PRESERVED (review transitions
            # are not failure evidence; only complete_task resets the breaker).
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL " + (", assignee = ?" if implementer else "") +
            " WHERE id = ? AND status = 'review'",
            params,
        )
        if cur.rowcount != 1:
            raise WatchdogStateError(f"task {task_id} left review concurrently — re-read it")
        _kb._append_event(conn, task_id, "changes_requested", {
            "reviewer": decider, "reason": note, "via": "watchdog",
            "watchdog_id": wd["id"],
        })
        return {"status": new_status, "implementer": implementer}

    if verb == "reopen":
        if status == "review":
            # review_reopened semantics (same shape as reopen_review_task).
            _kb._reclaim_dangling_run(conn, task_id, statuses=("review",), now=now,
                                       note="watchdog reopen")
            new_status = _kb._landing_status_after_parents(conn, task_id)
            requested = _kb._latest_event(conn, task_id, "review_requested")
            implementer = None
            if requested is not None:
                implementer = _kb._nonblank_str(
                    _kb._json_dict(_kb._row_get(requested, "payload")).get("implementer"))
            params = (new_status, *((implementer,) if implementer else ()), task_id)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL " + (", assignee = ?" if implementer else "") +
                " WHERE id = ? AND status = 'review'",
                params,
            )
            if cur.rowcount != 1:
                raise WatchdogStateError(f"task {task_id} left review concurrently — re-read it")
            payload: dict[str, Any] = {"status": new_status, "via": "watchdog",
                                       "watchdog_id": wd["id"]}
            if implementer:
                payload["implementer"] = implementer
            _kb._append_event(conn, task_id, "review_reopened", payload)
            return {"status": new_status, "implementer": implementer}
        if status == "blocked":
            block_kind = task.block_kind
            if block_kind not in REOPENABLE_BLOCK_KINDS:
                raise PermissionError(
                    f"reopen refuses the human gate {block_kind!r}: needs_input/capability "
                    "blocks open only by an explicit human unblock or an approval "
                    "decision (spec §12-6) — the watchdog never opens them")
            if not _kb._unblock_task_in_txn(conn, task_id):
                raise WatchdogStateError(
                    f"task {task_id} left blocked concurrently — re-read it")
            # Provenance rides on the watchdog_decided event; the unblocked
            # event carries the transition itself.
            return {"block_kind": block_kind}
        raise WatchdogStateError(f"reopen applies to stopped tasks only (task is {status!r})")

    # verb == "reassign": the standard assign semantics, inline so the
    # decision commits atomically with the firing outcome.
    assignee_value = str(assignee or "").strip()
    if not assignee_value:
        raise ValueError("reassign requires the new assignee profile")
    new_assignee = _kb._canonical_assignee(assignee_value)
    if new_assignee == task.assignee:
        raise WatchdogStateError(
            f"reassign to the current assignee {new_assignee!r} is a no-op")
    if task.claim_lock is not None and status == "running":
        raise WatchdogStateError(
            "cannot reassign: task is running under a live claim")
    conn.execute(
        "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
        "last_failure_error = NULL WHERE id = ?",
        (new_assignee, task_id),
    )
    _kb._append_event(conn, task_id, "assigned", {
        "assignee": new_assignee, "from": task.assignee,
        "via": "watchdog", "watchdog_id": wd["id"],
    })
    return {"assignee": new_assignee}


# ---------------------------------------------------------------------------
# CLI surface: ``hermes kanban watchdog create|list|show|rm|check|decide``
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


def dispatch_watchdog(args: argparse.Namespace) -> int:
    """``hermes kanban watchdog …`` — the human watchdog surface.

    ``decide`` is the apply path; the worker toolset exposes the same kernel
    fenced to the watchdog's own reviewer profile. ``check`` runs the tick
    phase on demand (the dispatcher runs it only with
    ``kanban.watchdog.tick_enabled``).
    """
    from hermes_cli import kanban_db_connect as _kbc

    action = getattr(args, "watchdog_action", None) or "list"
    board = _resolve_board_slug(getattr(args, "board", None))
    as_json = bool(getattr(args, "json", False))

    if action == "create":
        try:
            with _kbc.connect_closing() as conn:
                result = create_watchdog(
                    conn, board=board, task_id=args.task,
                    reviewer=args.reviewer, instructions=getattr(args, "instructions", None),
                    created_by=_acting_profile(), via="cli",
                )
        except ValueError as exc:
            return _cli_err(str(exc))
        except PermissionError as exc:
            print(f"kanban: {exc}")
            return 2
        except LookupError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        print(f"watchdog {result['id']}: watching task {result['task_id']} "
              f"(reviewer {result['reviewer']})")
        print(f"decide via: hermes kanban watchdog decide {result['id']} "
              "--verb accept|request_changes|reopen|reassign [--note ...]")
        return 0

    if action == "list":
        try:
            with _kbc.connect_closing() as conn:
                rows = list_watchdogs(conn, board, status=getattr(args, "status", None))
        except ValueError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print(f"No watchdogs on board {board} (hermes kanban watchdog create …)")
            return 0
        print(f"Board: {board}   ({len(rows)} watchdog(s))")
        print(f"{'id':21s} {'task':18s} {'reviewer':14s} {'status':9s} escalated")
        for row in rows:
            print(f"{row['id']:21s} {row['task_id']:18s} {row['reviewer']:14s} "
                  f"{row['status']:9s} {'-' if row['escalated_at'] is None else 'sticky'}")
        return 0

    if action == "show":
        ref = args.watchdog_id
        try:
            with _kbc.connect_closing() as conn:
                row = get_watchdog(conn, ref)
                firings = list_firings(conn, board, watchdog_id=row["id"])
        except LookupError:
            # also accept a task id as the reference (its active watchdog)
            try:
                with _kbc.connect_closing() as conn:
                    row = watchdog_for_task(conn, board, ref)
                    if row is None:
                        raise LookupError(ref)
                    firings = list_firings(conn, board, watchdog_id=row["id"])
            except LookupError as exc:
                return _cli_err(f"watchdog {exc}")
        if as_json:
            print(json.dumps({"watchdog": row, "firings": firings}, indent=2, sort_keys=True))
            return 0
        print(f"Watchdog: {row['id']}   Board: {row['board']}   Status: {row['status']}")
        print(f"task={row['task_id']} reviewer={row['reviewer']} "
              f"created_by={row['created_by']}")
        if row["instructions"]:
            print(f"instructions: {row['instructions']}")
        if row["escalated_at"] is not None:
            print(f"ESCALATED (sticky) at {row['escalated_at']} — human action required")
        if not firings:
            print("firings: none (the task has not stopped since the watchdog was attached)")
            return 0
        print("firings:")
        for fr in firings:
            state = fr["outcome"] or f"pending (rounds {fr['rounds']})"
            print(f"  [{fr['fired_at']}] {fr['stop_kind']} fp={fr['fingerprint'][:12]}… "
                  f"rounds={fr['rounds']} -> {state}"
                  + (f" by {fr['decided_by']} via {fr['decided_via']}" if fr["outcome"] else ""))
        return 0

    if action == "rm":
        try:
            with _kbc.connect_closing() as conn:
                result = remove_watchdog(conn, args.watchdog_id, removed_by=_acting_profile())
        except LookupError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        print(f"watchdog {result['id']}: retired")
        return 0

    if action == "check":
        try:
            with _kbc.connect_closing() as conn:
                report = check_watchdogs(conn, board, task_id=getattr(args, "task", None))
        except ValueError as exc:
            return _cli_err(str(exc))
        if as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        print(f"watchdog check: checked={report['checked']} fired={len(report['fired'])} "
              f"rounds={len(report['rounds'])} escalated={len(report['escalated'])}")
        for entry in report["fired"]:
            print(f"  fired {entry['watchdog_id']} on {entry['task_id']} ({entry['stop_kind']})")
        for entry in report["rounds"]:
            print(f"  identical round {entry['round']} on {entry['task_id']}")
        for entry in report["escalated"]:
            print(f"  ESCALATED {entry['watchdog_id']} on {entry['task_id']} "
                  f"(rounds {entry['rounds']}) — sticky, human action required")
        return 0

    if action == "decide":
        try:
            with _kbc.connect_closing() as conn:
                result = decide_firing(
                    conn, args.watchdog_id, verb=args.verb, decider=_acting_profile(),
                    note=getattr(args, "note", None),
                    assignee=getattr(args, "assignee", None), via="cli",
                )
        except LookupError as exc:
            return _cli_err(str(exc))
        except WatchdogStateError as exc:
            return _cli_err(str(exc))
        except PermissionError as exc:
            print(f"kanban: {exc}")
            return 2
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        print(f"watchdog {result['watchdog']['id']}: {args.verb} -> "
              f"{result['firing']['outcome']} by {result['firing']['decided_by']}")
        return 0

    return _cli_err(f"unknown watchdog action {action!r}")
