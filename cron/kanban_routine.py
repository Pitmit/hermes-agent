"""Kanban routines — deterministic cron→kanban occurrences (governance stage 6).

Spec: ``docs/kanban-governance-spec.md`` §8 ("Routines: Cron→Kanban ohne zweite
Scheduler-Engine"). A cron job with an additive ``kanban`` block NEVER wakes an
agent: each scheduled occurrence materializes as ONE kanban task created through
the normal kernel (``kanban_db.create_task``), and the dispatcher spawns the
assigned worker exactly as for a hand-created card. The cron engine stays the
only clock — no second scheduler loop, no dispatcher changes.

Determinism / exactly-once per occurrence:

* the idempotency key is derived deterministically from
  ``(job_id, scheduled_instant)`` — ``routine:{job_id}:{scheduled_instant}`` by
  default — where ``scheduled_instant`` is the canonical UTC instant the cron
  occurrence ledger already guarantees at-most-once for (#107485). The key is
  stable across retries, crash re-fires and lost answers: a re-fire reads the
  existing task back through ``tasks.idempotency_key`` (the same dedupe
  ``create_task`` applies) and creates nothing.
* catch-up policy (``kanban.catch_up``) decides what a MISSED occurrence
  becomes when the scheduler was down (the fire is classified ``catch_up`` by
  the cron due scan, ``last_dispatch.kind``):

  - ``skip`` — the missed occurrence is deliberately NOT materialized (stale
    work is worthless); the run is a logged no-op.
  - ``once`` (default) — exactly ONE catch-up card for the gap (cron already
    fires once for accumulated misses).
  - ``all-bounded`` — one card per missed occurrence, walking the schedule
    backwards from the fired instant, bounded by ``catch_up_bound`` (default
    5, max 20) so an outage can never flood the board. The walk stops at the
    first occurrence that already has a card (the gap boundary).

* ``no_agent`` routines stay deterministic scripts: the script IS the job
  (stdout delivered as usual) and kanban work appears ONLY on exception — a
  failed script run creates one exception card (same occurrence key, so a
  retry of the same occurrence never double-cards).

No kanban.db schema change: dedupe rides on the existing
``tasks.idempotency_key`` column; provenance rides on ``task_events`` (kind
``routine_occurrence`` carries job id, scheduled instant, key and dispatch
classification).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from cron.occurrences import scheduled_instant as _canonical_instant
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

try:  # croniter is a core dependency but imported lazily everywhere in cron/
    from croniter import croniter as _croniter_cls
except Exception:  # pragma: no cover - croniter missing only in broken envs
    _croniter_cls = None

#: ``task_events`` kind written on every MATERIALIZED routine occurrence
#: (provenance: job id, canonical scheduled instant, idempotency key, dispatch
#: classification). Written only on creation — a deduplicated re-fire appends
#: nothing, so the audit trail stays exactly-once too.
ROUTINE_EVENT = "routine_occurrence"

#: Catch-up policies for missed occurrences (see module docstring).
CATCH_UP_POLICIES = ("skip", "once", "all-bounded")
DEFAULT_CATCH_UP = "once"
DEFAULT_CATCH_UP_BOUND = 5
MAX_CATCH_UP_BOUND = 20

#: Default idempotency-key template: one stable key per scheduled occurrence.
DEFAULT_IDEMPOTENCY_TEMPLATE = "routine:{job_id}:{scheduled_instant}"

VALID_WORKSPACE_KINDS = ("scratch", "dir", "worktree")

#: Key infix for manual fires (``hermes cron run`` / cronjob action="run"): an
#: explicit human action has no occurrence identity, so it keys on the fire
#: time — every manual fire is a deliberate extra occurrence.
_MANUAL_INFIX = "manual"

#: Excerpt cap for script-failure evidence embedded in the exception card.
_FAILURE_EXCERPT_CHARS = 4000


# ---------------------------------------------------------------------------
# Block normalization (create/update paths share one chokepoint)
# ---------------------------------------------------------------------------

def normalize_kanban_block(block: Any) -> Optional[Dict[str, Any]]:
    """Validate and canonicalize a job's ``kanban`` block; None clears/absent.

    Raises ``ValueError`` (fail-closed, nothing persisted) on any invalid shape.
    Only explicitly set keys survive: an absent key keeps its runtime default.
    """
    if block is None:
        return None
    if isinstance(block, str) and not block.strip():
        return None
    if not isinstance(block, dict):
        raise ValueError("kanban block must be a mapping of routine fields")

    out: Dict[str, Any] = {}

    board = str(block.get("board") or "").strip()
    if not board:
        raise ValueError("kanban routine requires 'board' (the kanban board slug)")
    out["board"] = board

    title = str(block.get("title") or "").strip()
    if not title:
        raise ValueError(
            "kanban routine requires 'title' (the recurring task title; "
            "'{date}' is replaced with the occurrence's UTC date)")
    out["title"] = title

    body_file = block.get("body_file")
    body_inline = block.get("body_inline")
    if body_file is not None and body_inline is not None:
        raise ValueError("body_file and body_inline are mutually exclusive")
    if body_file is not None:
        body_file = str(body_file).strip()
        if not body_file:
            raise ValueError("body_file must be a non-empty path when set")
        if not body_file.startswith(("/", "~")):
            raise ValueError("body_file must be an absolute path (read at fire time)")
        out["body_file"] = body_file
    if body_inline is not None:
        body_inline = str(body_inline)
        if not body_inline.strip():
            raise ValueError("body_inline must be non-empty when set")
        out["body_inline"] = body_inline

    assignee = block.get("assignee")
    if assignee is not None:
        assignee = str(assignee).strip()
        if not assignee:
            raise ValueError("assignee must be a non-empty profile name when set")
        out["assignee"] = assignee

    priority = block.get("priority")
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise ValueError("kanban routine priority must be an integer") from None
        out["priority"] = priority

    catch_up = block.get("catch_up")
    if catch_up is not None:
        catch_up = str(catch_up).strip()
        if catch_up not in CATCH_UP_POLICIES:
            raise ValueError(
                f"kanban routine catch_up must be one of {list(CATCH_UP_POLICIES)}, "
                f"got {catch_up!r}")
        out["catch_up"] = catch_up

    bound = block.get("catch_up_bound")
    if bound is not None:
        try:
            bound = int(bound)
        except (TypeError, ValueError):
            raise ValueError("kanban routine catch_up_bound must be an integer") from None
        if not 1 <= bound <= MAX_CATCH_UP_BOUND:
            raise ValueError(
                f"kanban routine catch_up_bound must be within 1..{MAX_CATCH_UP_BOUND}, "
                f"got {bound}")
        out["catch_up_bound"] = bound

    key_template = block.get("idempotency_key")
    if key_template is not None:
        key_template = str(key_template).strip()
        if not key_template:
            raise ValueError("idempotency_key must be a non-empty template when set")
        if "{scheduled_instant}" not in key_template:
            raise ValueError(
                "custom idempotency_key must contain {scheduled_instant} so every "
                "occurrence dedupes on its own stable key (a template without it "
                "would collapse all occurrences into one task)")
        out["idempotency_key"] = key_template

    workspace = block.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, dict):
            raise ValueError("kanban routine workspace must be a mapping {kind, path}")
        kind = str(workspace.get("kind") or "scratch").strip() or "scratch"
        if kind not in VALID_WORKSPACE_KINDS:
            raise ValueError(
                f"kanban routine workspace.kind must be one of {list(VALID_WORKSPACE_KINDS)}, "
                f"got {kind!r}")
        path = workspace.get("path")
        if path is not None:
            path = str(path).strip() or None
            if path and kind == "scratch":
                raise ValueError("workspace path requires workspace.kind 'dir' or 'worktree'")
        ws_out: Dict[str, Any] = {"kind": kind}
        if path:
            ws_out["path"] = path
        out["workspace"] = ws_out

    return out


def validate_kanban_job_shape(
    *, prompt: Any, script: Any, no_agent: Any,
    monitor_script: Any, monitor_url: Any, kanban: Optional[Dict[str, Any]],
) -> None:
    """Cross-field invariants between the kanban block and the job's own payload.

    A kanban routine never wakes an agent, so everything that only an agent run
    would consume is refused fail-closed at create AND at update.
    """
    if kanban is None:
        return
    if str(prompt or "").strip():
        raise ValueError(
            "a kanban routine never wakes an agent — its work description belongs in "
            "kanban body_inline/body_file, not in a prompt the routine would ignore")
    if str(monitor_script or "").strip() or str(monitor_url or "").strip():
        raise ValueError(
            "monitor sources gate AGENT runs and are incompatible with a kanban "
            "routine (no agent run happens)")
    if str(script or "").strip() and not no_agent:
        raise ValueError(
            "a kanban routine script requires no_agent=True — without an agent run "
            "there is no prompt for script stdout. With no_agent the script is the "
            "routine's deterministic check; its failure creates the exception card")


# ---------------------------------------------------------------------------
# Occurrence identity
# ---------------------------------------------------------------------------

def _occurrence_date(scheduled_instant: str) -> str:
    """UTC calendar date of an occurrence, for the ``{date}`` placeholder."""
    try:
        return datetime.fromisoformat(str(scheduled_instant)).astimezone(
            timezone.utc).date().isoformat()
    except ValueError:
        return str(scheduled_instant)


def routine_idempotency_key(
    block: Dict[str, Any], job_id: str, scheduled_instant: Optional[str],
) -> str:
    """The stable per-occurrence key: template substitution, never ``format()``
    (a routine title/body is user data; ``str.replace`` cannot raise on braces)."""
    template = block.get("idempotency_key") or DEFAULT_IDEMPOTENCY_TEMPLATE
    return (template
            .replace("{job_id}", str(job_id))
            .replace("{scheduled_instant}", str(scheduled_instant))
            .replace("{date}", _occurrence_date(str(scheduled_instant))))


def _render(text: str, scheduled_instant: str) -> str:
    return str(text).replace("{date}", _occurrence_date(scheduled_instant))


def _dispatch_kind(job: Dict[str, Any], scheduled_instant: Optional[str]) -> str:
    """``on_time`` / ``late`` / ``catch_up`` for this occurrence, or ``manual``.

    Only the dispatch snapshot whose ``scheduled_at`` IS this occurrence is
    trusted (``last_dispatch`` survives the run but describes one fire; a
    stale stamp from an earlier occurrence must never classify this one).
    """
    if not scheduled_instant:
        return "manual"
    dispatch = job.get("last_dispatch")
    if isinstance(dispatch, dict):
        if _canonical_instant(dispatch.get("scheduled_at")) == scheduled_instant:
            kind = dispatch.get("kind")
            if kind in ("on_time", "late", "catch_up"):
                return str(kind)
    return "on_time"


def previous_instants(
    schedule: Dict[str, Any], scheduled_instant: str, count: int,
) -> List[str]:
    """Up to ``count`` occurrence instants strictly BEFORE ``scheduled_instant``,
    newest first. Mirrors ``jobs.compute_next_run``'s wall-clock handling for
    cron expressions (configured zone, DST fold guard) so the walk lands on the
    same lattice the scheduler fires on. Unsupported shapes return [] — the
    caller then simply materializes only the fired occurrence."""
    if count <= 0 or not isinstance(schedule, dict):
        return []
    try:
        fired = datetime.fromisoformat(str(scheduled_instant))
    except ValueError:
        return []
    if fired.tzinfo is None:
        return []
    kind = schedule.get("kind")
    out: List[str] = []
    if kind == "interval":
        minutes = schedule.get("minutes")
        if not minutes:
            return []
        for i in range(1, count + 1):
            prev = (fired.astimezone(timezone.utc) - timedelta(minutes=minutes * i)
                    ).astimezone(fired.tzinfo)
            out.append(prev.isoformat())
        return out
    if kind == "cron":
        expr = schedule.get("expr")
        if not expr or _croniter_cls is None:
            return []
        from hermes_time import get_timezone

        zone = get_timezone() or fired.tzinfo
        base_ts = fired.timestamp()
        it = _croniter_cls(expr, fired.astimezone(zone).replace(tzinfo=None))
        for _ in range(count):
            prev_wall = it.get_prev(datetime)
            found = None
            # DST fall-back: a repeated wall hour has two instants; prefer the
            # later one that is still strictly before the fired occurrence.
            for fold in (1, 0):
                candidate = prev_wall.replace(tzinfo=zone, fold=fold)
                if candidate.timestamp() < base_ts:
                    found = candidate
                    break
            if found is None:
                continue
            out.append(found.isoformat())
        return out
    return []


def occurrences_to_materialize(
    job: Dict[str, Any], block: Dict[str, Any], scheduled_instant: Optional[str],
    dispatch_kind: str, has_task: Callable[[str], bool],
) -> Tuple[List[Tuple[Optional[str], str]], Dict[str, Any]]:
    """Apply the catch-up policy: which occurrences this fire materializes.

    Returns ``(occurrences, note)`` where each occurrence is
    ``(canonical_instant_or_None_for_manual, key_infix_for_manual)`` — actually
    ``(instant, key)`` pairs with ``instant=None`` marking a manual fire —
    oldest first, plus an audit/receipt note. Never more than
    ``catch_up_bound`` entries: the board cannot be flooded by an outage.
    """
    policy = block.get("catch_up") or DEFAULT_CATCH_UP
    if dispatch_kind == "manual":
        # A manual fire has no occurrence identity: key on the fire time so
        # every deliberate extra occurrence gets its own card.
        manual_stamp = _hermes_now().astimezone(timezone.utc).isoformat()
        return [(None, f"{_MANUAL_INFIX}:{manual_stamp}")], {
            "policy": policy, "kind": "manual"}

    instant = str(scheduled_instant)
    if dispatch_kind != "catch_up":
        return [(instant, instant)], {"policy": policy, "kind": dispatch_kind}

    if policy == "skip":
        return [], {
            "policy": "skip", "kind": dispatch_kind,
            "reason": ("missed occurrence deliberately not materialized "
                       "(catch-up policy 'skip')")}
    if policy == "once":
        return [(instant, instant)], {
            "policy": "once", "kind": dispatch_kind,
            "reason": "one catch-up card for the accumulated gap"}

    # all-bounded: walk the schedule backwards, skipping occurrences that
    # already have a card (gap boundary), bounded so an outage never floods.
    bound = block.get("catch_up_bound") or DEFAULT_CATCH_UP_BOUND
    job_id = str(job.get("id") or "")
    backfill: List[Tuple[str, str]] = []
    for prev in previous_instants(job.get("schedule") or {}, instant, bound):
        if len(backfill) >= bound - 1:
            break
        canonical = _canonical_instant(prev)
        if canonical is None:
            continue
        if has_task(routine_idempotency_key(block, job_id, canonical)):
            break
        backfill.append((canonical, canonical))
    return backfill + [(instant, instant)], {
        "policy": "all-bounded", "kind": dispatch_kind, "bound": bound,
        "backfilled": len(backfill)}


# ---------------------------------------------------------------------------
# Materialization (board side)
# ---------------------------------------------------------------------------

def _routine_body(
    block: Dict[str, Any], scheduled_instant: str, exception_note: Optional[str],
) -> Optional[str]:
    body: Optional[str] = None
    body_file = block.get("body_file")
    if body_file:
        from pathlib import Path

        path = Path(body_file).expanduser()
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"kanban routine body_file {body_file!r} unreadable: {exc}") from exc
    elif block.get("body_inline"):
        body = str(block["body_inline"])
    body = _render(body, scheduled_instant) if body else None
    if exception_note:
        head = body or ""
        return (f"{head}\n\n" if head else "") + exception_note
    return body


def _find_routine_task(conn, key: str) -> Optional[Dict[str, Any]]:
    """Read-back of an existing (non-archived) task with this occurrence key —
    the healing path for a lost answer: the card exists, only the response
    never made it out. Same predicate ``create_task`` applies."""
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (key,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "status": row["status"]}


def materialize_occurrence(
    conn, *, job: Dict[str, Any], block: Dict[str, Any],
    occurrence_instant: Optional[str], key_infix: str, dispatch_kind: str,
    exception_note: Optional[str] = None,
) -> Dict[str, Any]:
    """Create (or read back) the ONE task for a single routine occurrence.

    Card + provenance event commit atomically in one transaction. A task with
    this occurrence key that already exists — open or done, e.g. after a crash
    between commit and response — is returned, never duplicated.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc

    job_id = str(job.get("id") or "")
    effective_instant = occurrence_instant or _hermes_now().astimezone(
        timezone.utc).isoformat()
    key = routine_idempotency_key(block, job_id, key_infix)
    existing = _find_routine_task(conn, key)
    if existing is not None:
        return {"task_id": existing["id"], "created": False, "key": key,
                "status": existing["status"]}

    title = _render(str(block["title"]), effective_instant)
    if exception_note:
        title = f"{title} — routine exception"
    body = _routine_body(block, effective_instant, exception_note)
    ws = block.get("workspace") or {}

    with _kbc.write_txn(conn):
        # create_task opens its own nested savepoint (allow_nested=True), so
        # the INSERT and the provenance event below share ONE commit.
        task_id = _kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=block.get("assignee"),
            created_by=f"cron:{job_id}",
            priority=int(block.get("priority") or 0),
            workspace_kind=ws.get("kind", "scratch"),
            workspace_path=ws.get("path"),
            idempotency_key=key,
            board=block.get("board"),
        )
        _kb._append_event(
            conn, task_id, ROUTINE_EVENT,
            {
                "routine_job_id": job_id,
                "routine_name": job.get("name"),
                "scheduled_instant": effective_instant if occurrence_instant else None,
                "manual": occurrence_instant is None,
                "idempotency_key": key,
                "dispatch_kind": dispatch_kind,
                "exception": bool(exception_note),
                "via": "cron",
            })
    return {"task_id": task_id, "created": True, "key": key, "status": None}


# ---------------------------------------------------------------------------
# The scheduler bridge (one _RunResult per fire; never a second loop)
# ---------------------------------------------------------------------------

def _failure_excerpt(text: str) -> str:
    text = str(text or "").strip()
    if len(text) > _FAILURE_EXCERPT_CHARS:
        return text[:_FAILURE_EXCERPT_CHARS] + "\n… (truncated)"
    return text


def _exception_note(job: Dict[str, Any], evidence: str, effective_instant: str) -> str:
    return (
        "## Routine script failure\n\n"
        f"The routine's deterministic script FAILED for occurrence "
        f"{effective_instant}.\n\n"
        "**Script output / error:**\n\n```\n"
        f"{_failure_excerpt(evidence)}\n```\n\n"
        f"This card was created automatically by kanban routine "
        f"{str(job.get('name') or '')!r} (cron job {job.get('id')})."
    )


def _connect_board(block: Dict[str, Any]):
    from hermes_cli.kanban_db_connect import connect_closing

    return connect_closing(board=block["board"])


def run_kanban_routine_job(
    job: dict, job_id: str, job_name: str, cancel_event,
) -> tuple:
    """Bridge entry used by ``scheduler._prepare_job_prompt``: returns the same
    ``_RunResult`` shape the no_agent short-circuit returns. Task-mode jobs
    materialize occurrences directly; ``no_agent`` jobs run their script first
    (unchanged semantics — the script IS the job) and create a card ONLY when
    the script fails."""
    from cron.scheduler import SILENT_MARKER

    block_raw = job.get("kanban")
    now_iso = _hermes_now().strftime("%Y-%m-%d %H:%M:%S")
    header = _routine_doc_header(job_name, job_id, now_iso, block_raw)

    try:
        block = normalize_kanban_block(block_raw)
    except ValueError as exc:
        logger.error("Job '%s': invalid kanban block — %s", job_id, exc)
        return (False, f"{header}**Status:** invalid kanban block\n\n{exc}\n",
                "", f"invalid kanban block: {exc}")
    if block is None:  # defensive: the hook only fires on a present block
        return (False, f"{header}**Status:** kanban block missing\n", "",
                "kanban block missing")

    scheduled_instant = job.get("_scheduled_instant")
    dispatch_kind = _dispatch_kind(job, scheduled_instant)

    # ---- no_agent routine: the script stays the job; card only on exception.
    if job.get("no_agent"):
        from cron.scheduler import _run_no_agent_job

        ok, doc, deliverable, error = _run_no_agent_job(job, job_id, job_name, cancel_event)
        if ok:
            return ok, doc, deliverable, error
        if dispatch_kind == "catch_up" and (block.get("catch_up") or DEFAULT_CATCH_UP) == "skip":
            logger.info(
                "Job '%s': routine script failed on a skipped catch-up occurrence "
                "(%s) — no exception card (policy 'skip')", job_id, scheduled_instant)
            return ok, doc, deliverable, error
        evidence = _failure_excerpt(deliverable or error or doc)
        effective_instant = scheduled_instant or _hermes_now().astimezone(
            timezone.utc).isoformat()
        manual_stamp = f"{_MANUAL_INFIX}:{effective_instant}" if not scheduled_instant else None
        note = _exception_note(job, evidence, effective_instant)
        try:
            with _connect_board(block) as conn:
                outcome = materialize_occurrence(
                    conn, job=job, block=block,
                    occurrence_instant=scheduled_instant,
                    key_infix=scheduled_instant or manual_stamp,
                    dispatch_kind=dispatch_kind, exception_note=note)
        except Exception as exc:
            # The script failure is already reported through the normal cron
            # failure lane; a board problem on top is logged, never masking it.
            logger.exception("Job '%s': routine exception card failed: %s", job_id, exc)
            return ok, doc, deliverable, error
        card_line = (
            f"routine exception card {outcome['task_id']}"
            + (" (deduplicated: existing card)" if not outcome["created"] else ""))
        logger.info("Job '%s': script failure created %s", job_id, card_line)
        return ok, f"{doc}\n\n**Kanban routine:** created {card_line}\n", deliverable, error

    # ---- task-mode routine: the occurrence IS the work; no agent, no script.
    try:
        with _connect_board(block) as conn:
            occurrences, note = occurrences_to_materialize(
                job, block, scheduled_instant, dispatch_kind,
                lambda key: _find_routine_task(conn, key) is not None)
            lines: List[str] = []
            created = deduped = 0
            for occurrence_instant, key_infix in occurrences:
                outcome = materialize_occurrence(
                    conn, job=job, block=block,
                    occurrence_instant=occurrence_instant, key_infix=key_infix,
                    dispatch_kind=dispatch_kind)
                if outcome["created"]:
                    created += 1
                else:
                    deduped += 1
                lines.append(
                    f"- occurrence {occurrence_instant or key_infix} → task "
                    f"{outcome['task_id']}"
                    + (" (deduplicated: open identical run)" if not outcome["created"] else ""))
            if not occurrences:
                lines.append(f"- {note.get('reason', 'no occurrence materialized')}")
    except Exception as exc:
        logger.exception("Job '%s': kanban routine failed", job_id)
        return (False, f"{header}**Status:** routine failed\n\n{exc}\n", "",
                f"kanban routine failed: {exc}")

    status = (f"**Status:** materialized {created} occurrence(s)"
              + (f", deduplicated {deduped}" if deduped else "") + "\n\n")
    doc = header + status + "\n".join(lines) + "\n"
    receipt = (f"Kanban routine '{job_name}': {created} task(s) created"
               + (f", {deduped} deduplicated" if deduped else "")
               + (f" — {note.get('reason')}" if note.get("reason") else "")
               + ".")
    logger.info("Job '%s': %s", job_id, receipt)
    if not created and not deduped:
        # A policy skip is deliberate governance, not a failure — but it must
        # never be silent either: the receipt is delivered like any run output.
        return True, doc, receipt, None
    return True, doc, receipt, None


def _routine_doc_header(job_name: str, job_id: str, now_iso: str, block_raw: Any) -> str:
    board = block_raw.get("board") if isinstance(block_raw, dict) else None
    return (
        f"# Cron Job: {job_name}\n\n"
        f"**Job ID:** {job_id}\n"
        f"**Run Time:** {now_iso}\n"
        f"**Mode:** kanban routine (board: {board})\n"
    )