"""Kanban routines — deterministic cron→kanban occurrences (governance stage 6).

Spec: ``docs/kanban-governance-spec.md`` §8. These tests pin the contract the
spec demands:

* exactly-once per scheduled occurrence — the idempotency key
  ``routine:{job_id}:{scheduled_instant}`` is stable across crash re-fires and
  lost answers (read-back), so N occurrences produce exactly N tasks;
* catch-up policies ``skip`` / ``once`` / ``all-bounded`` for MISSED
  occurrences, with the bound proving an outage can never flood the board;
* canonical-UTC occurrence identity across DST transitions;
* ``no_agent`` routines stay deterministic scripts and create kanban work ONLY
  on exception;
* pre-feature cron jobs (no ``kanban`` block) are byte-identical and dispatch
  through their old paths;
* CLI / API / tool create+read paths share the one validation chokepoint.

The board is the real per-test ``HERMES_HOME`` board (E2E resolution through
``kanban_db_path``), never a mocked connection.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cron.kanban_routine import (
    CATCH_UP_POLICIES,
    DEFAULT_CATCH_UP_BOUND,
    ROUTINE_EVENT,
    normalize_kanban_block,
    occurrences_to_materialize,
    previous_instants,
    routine_idempotency_key,
)
from cron.occurrences import scheduled_instant as canonical_instant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BLOCK = {"board": "default", "title": "Weekly maintenance {date}"}


def _job(job_id="routinejob", *, block=None, instant=None, dispatch_kind=None,
         schedule=None, no_agent=False, script=None, name="routine"):
    job = {
        "id": job_id, "name": name, "kanban": dict(block if block is not None else BLOCK),
        "schedule": schedule or {"kind": "interval", "minutes": 60},
        "no_agent": no_agent, "prompt": "", "deliver": "local",
        "_scheduled_instant": instant,
    }
    if dispatch_kind:
        job["last_dispatch"] = {
            "scheduled_at": instant,
            "dispatched_at": instant,
            "lateness_seconds": 999999.0,
            "kind": dispatch_kind,
        }
    if script:
        job["script"] = script
    return job


def _fire(job):
    """Run one routine fire through the REAL dispatch seam
    (``scheduler._prepare_job_prompt``) — the same entry the tick uses."""
    import cron.scheduler as sched

    return sched._prepare_job_prompt(job, job["id"], job.get("name", "job"), None, None)[0]


def _tasks(conn, key_prefix):
    return conn.execute(
        "SELECT id, idempotency_key, status FROM tasks WHERE idempotency_key LIKE ? "
        "ORDER BY id", (key_prefix + "%",)).fetchall()


def _connect():
    from hermes_cli.kanban_db_connect import connect

    return connect(board="default")


def _write_script(name: str, body: str) -> str:
    from hermes_constants import get_hermes_home

    scripts = get_hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / name
    path.write_text(body)
    path.chmod(0o700)
    return name


# ---------------------------------------------------------------------------
# Block validation (the one chokepoint for CLI / API / tool)
# ---------------------------------------------------------------------------

class TestBlockValidation:
    @pytest.mark.parametrize("bad,match", [
        ({"board": "default"}, "title"),
        ({"title": "t"}, "board"),
        ({"board": "b", "title": "t", "body_file": "/x", "body_inline": "y"}, "mutually exclusive"),
        ({"board": "b", "title": "t", "body_file": "relative.md"}, "absolute"),
        ({"board": "b", "title": "t", "catch_up": "always"}, "catch_up"),
        ({"board": "b", "title": "t", "catch_up_bound": 0}, "catch_up_bound"),
        ({"board": "b", "title": "t", "catch_up_bound": 99}, "catch_up_bound"),
        ({"board": "b", "title": "t", "idempotency_key": "static"}, "{scheduled_instant}"),
        ({"board": "b", "title": "t", "workspace": {"kind": "bogus"}}, "workspace"),
        ({"board": "b", "title": "t", "workspace": {"kind": "scratch", "path": "/x"}}, "workspace"),
        ("not-a-mapping", "mapping"),
    ])
    def test_invalid_blocks_refused(self, bad, match):
        with pytest.raises(ValueError, match=match):
            normalize_kanban_block(bad)

    def test_none_and_empty_clear(self):
        assert normalize_kanban_block(None) is None
        assert normalize_kanban_block("") is None

    def test_minimal_block_canonical(self):
        assert normalize_kanban_block({"board": "b", "title": " t "}) == {
            "board": "b", "title": "t"}


class TestJobShapeValidation:
    def test_prompt_refused(self):
        from cron.kanban_routine import validate_kanban_job_shape

        with pytest.raises(ValueError, match="never wakes an agent"):
            validate_kanban_job_shape(
                prompt="do things", script=None, no_agent=False,
                monitor_script=None, monitor_url=None, kanban={"board": "b", "title": "t"})

    def test_monitor_refused(self):
        from cron.kanban_routine import validate_kanban_job_shape

        with pytest.raises(ValueError, match="monitor"):
            validate_kanban_job_shape(
                prompt="", script=None, no_agent=False,
                monitor_script="/tmp/x.sh", monitor_url=None,
                kanban={"board": "b", "title": "t"})

    def test_script_without_no_agent_refused(self):
        from cron.kanban_routine import validate_kanban_job_shape

        with pytest.raises(ValueError, match="no_agent=True"):
            validate_kanban_job_shape(
                prompt="", script="check.sh", no_agent=False,
                monitor_script=None, monitor_url=None,
                kanban={"board": "b", "title": "t"})

    def test_no_agent_script_allowed(self):
        from cron.kanban_routine import validate_kanban_job_shape

        validate_kanban_job_shape(
            prompt="", script="check.sh", no_agent=True,
            monitor_script=None, monitor_url=None, kanban={"board": "b", "title": "t"})


# ---------------------------------------------------------------------------
# API create/update (cron.jobs)
# ---------------------------------------------------------------------------

class TestApiCreateUpdate:
    def test_create_job_with_block(self):
        from cron.jobs import create_job, get_job

        job = create_job(
            prompt=None, schedule="0 9 * * *",
            kanban={"board": "default", "title": "Weekly check", "assignee": "worker-maintain"},
        )
        assert job["kanban"]["board"] == "default"
        # task-mode routine has no prompt/script/skills — the block IS the payload
        assert job["prompt"] == ""
        # the name falls back to the routine title
        assert job["name"] == "Weekly check"
        stored = get_job(job["id"])
        assert stored["kanban"] == job["kanban"]

    def test_create_job_conflicts_fail_closed(self):
        from cron.jobs import create_job

        with pytest.raises(ValueError, match="never wakes an agent"):
            create_job(prompt="hello", schedule="0 9 * * *",
                       kanban={"board": "default", "title": "Weekly check"})
        with pytest.raises(ValueError, match="monitor"):
            create_job(prompt=None, schedule="0 9 * * *",
                       monitor_url="https://example.com/feed",
                       kanban={"board": "default", "title": "Weekly check"})
        with pytest.raises(ValueError, match="no_agent=True"):
            create_job(prompt=None, schedule="0 9 * * *", script="check.sh",
                       kanban={"board": "default", "title": "Weekly check"})

    def test_legacy_job_has_no_kanban_key(self):
        from cron.jobs import create_job

        job = create_job(prompt="plain old job", schedule="0 9 * * *")
        assert "kanban" not in job
        # and the persisted record round-trips without the key
        import cron.jobs as jobs

        persisted = [j for j in jobs.load_jobs() if j["id"] == job["id"]][0]
        assert "kanban" not in persisted

    def test_update_replaces_and_clears_block(self):
        from cron.jobs import create_job, get_job, update_job

        job = create_job(prompt=None, schedule="0 9 * * *",
                         kanban={"board": "default", "title": "T1"})
        updated = update_job(job["id"], {"kanban": {"board": "other", "title": "T2",
                                                    "catch_up": "skip"}})
        assert updated["kanban"]["title"] == "T2"
        assert updated["kanban"]["catch_up"] == "skip"
        # clearing the block of a block-ONLY job is an empty payload — refused
        with pytest.raises(ValueError, match="nothing to run"):
            update_job(job["id"], {"kanban": None})
        assert get_job(job["id"])["kanban"]["title"] == "T2"  # the refused clear wrote nothing
        # a no_agent script routine keeps its script: clearing is fine there
        script = _write_script("api-clear.sh", "#!/usr/bin/env bash\nexit 0\n")
        script_job = create_job(prompt=None, schedule="0 9 * * *", no_agent=True,
                                script=script, kanban={"board": "default", "title": "T1"})
        cleared = update_job(script_job["id"], {"kanban": None})
        assert "kanban" not in cleared
        assert cleared["script"]

    def test_update_conflicting_fields_revalidated(self):
        from cron.jobs import create_job, update_job

        job = create_job(prompt=None, schedule="0 9 * * *",
                         kanban={"board": "default", "title": "T"})
        with pytest.raises(ValueError, match="never wakes an agent"):
            update_job(job["id"], {"prompt": "now with a prompt"})

    def test_job_definition_fields_cover_kanban(self):
        from cron.job_definition import JOB_DEFINITION_FIELDS

        assert "kanban" in JOB_DEFINITION_FIELDS


# ---------------------------------------------------------------------------
# Occurrence materialization (through the real scheduler seam)
# ---------------------------------------------------------------------------

class TestTaskModeOccurrences:
    INSTANT = "2026-09-28T09:00:00+00:00"

    def test_fire_creates_exactly_one_task(self):
        job = _job(instant=self.INSTANT, dispatch_kind="on_time")
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            rows = _tasks(conn, f"routine:{job['id']}:")
            assert len(rows) == 1
            assert rows[0]["idempotency_key"] == (
                f"routine:{job['id']}:{self.INSTANT}")
            from hermes_cli import kanban_db as kb

            task = kb.get_task(conn, rows[0]["id"])
            # '{date}' rendered from the occurrence's UTC date; assignee normalized
            assert task.title == "Weekly maintenance 2026-09-28"
            assert task.assignee is None  # unset in this block
            assert task.status == "ready"  # enters the normal dispatcher flow
        finally:
            conn.close()

    def test_retry_and_open_run_never_duplicate(self):
        """Crash re-fire while the identical run is OPEN, and again after it is
        done: the read-back returns the one card, never an N+1th."""
        job = _job(instant=self.INSTANT, dispatch_kind="on_time")
        first = _fire(job)
        second = _fire(job)  # simulated crash-after-commit re-fire
        assert first[0] and second[0]
        conn = _connect()
        try:
            rows = _tasks(conn, f"routine:{job['id']}:")
            assert len(rows) == 1
            # complete the occurrence's task, then re-fire once more
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (rows[0]["id"],))
            conn.commit()
        finally:
            conn.close()
        third = _fire(job)
        assert third[0]
        conn = _connect()
        try:
            assert len(_tasks(conn, f"routine:{job['id']}:")) == 1
        finally:
            conn.close()

    def test_lost_answer_readback(self):
        """The card already exists (response lost); the fire reports it instead
        of creating a duplicate."""
        from hermes_cli import kanban_db as kb

        key = f"routine:routinejob:{self.INSTANT}"
        conn = _connect()
        try:
            existing = kb.create_task(
                conn, title="Weekly maintenance 2026-09-28",
                idempotency_key=key, board="default")
        finally:
            conn.close()
        job = _job(instant=self.INSTANT, dispatch_kind="on_time")
        result = _fire(job)
        assert result[0] is True
        assert existing in result[1]  # the receipt names the existing card
        conn = _connect()
        try:
            assert len(_tasks(conn, "routine:routinejob:")) == 1
        finally:
            conn.close()

    def test_provenance_event_exactly_once(self):
        job = _job(instant=self.INSTANT, dispatch_kind="on_time")
        _fire(job)
        _fire(job)  # dedupe must not append a second audit event
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT payload FROM task_events WHERE kind = ?", (ROUTINE_EVENT,)).fetchall()
            assert len(rows) == 1
            payload = json.loads(rows[0]["payload"])
            assert payload["routine_job_id"] == "routinejob"
            assert payload["scheduled_instant"] == self.INSTANT
            assert payload["dispatch_kind"] == "on_time"
            assert payload["idempotency_key"] == (
                f"routine:routinejob:{self.INSTANT}")
        finally:
            conn.close()

    def test_manual_fire_keys_on_fire_time(self):
        job = _job()  # no _scheduled_instant: hermes cron run / cronjob run
        job.pop("last_dispatch", None)
        first = _fire(job)
        second = _fire(job)
        assert first[0] and second[0]
        conn = _connect()
        try:
            rows = _tasks(conn, "routine:routinejob:manual:")
            assert len(rows) == 2  # each deliberate manual fire is its own card
            assert len({r["id"] for r in rows}) == 2
        finally:
            conn.close()

    def test_board_assignee_priority_workspace(self):
        block = dict(BLOCK, assignee="Worker-Maintain", priority=42,
                     body_inline="Do the thing.",
                     workspace={"kind": "dir", "path": "/tmp"})
        job = _job(block=block, instant=self.INSTANT, dispatch_kind="on_time")
        _fire(job)
        conn = _connect()
        try:
            from hermes_cli import kanban_db as kb

            row = _tasks(conn, f"routine:{job['id']}:")[0]
            task = kb.get_task(conn, row["id"])
            assert task.assignee == "worker-maintain"
            assert task.priority == 42
            assert task.body == "Do the thing."
            assert task.workspace_kind == "dir"
            assert task.workspace_path == "/tmp"
        finally:
            conn.close()

    def test_body_file_rendered_and_missing_file_fails(self, tmp_path):
        body_file = tmp_path / "spec.md"
        body_file.write_text("Plan for {date}.\n")
        block = dict(BLOCK, body_file=str(body_file))
        job = _job(block=block, instant=self.INSTANT, dispatch_kind="on_time")
        assert _fire(job)[0] is True
        conn = _connect()
        try:
            from hermes_cli import kanban_db as kb

            row = _tasks(conn, f"routine:{job['id']}:")[0]
            assert kb.get_task(conn, row["id"]).body == "Plan for 2026-09-28.\n"
        finally:
            conn.close()
        # unreadable body_file = failed run (visible error), no card
        gone = _job(block=dict(BLOCK, body_file=str(tmp_path / "gone.md")),
                    instant="2026-09-29T09:00:00+00:00", dispatch_kind="on_time")
        result = _fire(gone)
        assert result[0] is False
        assert "unreadable" in (result[3] or "")

    def test_malformed_block_fails_closed(self):
        job = _job()
        job["kanban"] = {"board": "default"}  # title missing (hand-edited)
        result = _fire(job)
        assert result[0] is False
        assert "title" in (result[3] or "")


# ---------------------------------------------------------------------------
# Catch-up policies
# ---------------------------------------------------------------------------

class TestCatchUpPolicies:
    FIRED = "2026-09-28T12:00:00+00:00"

    def _caught_up_job(self, **block_extra):
        block = dict(BLOCK, **block_extra)
        return _job(instant=self.FIRED, dispatch_kind="catch_up", block=block)

    def test_all_three_policies_declared(self):
        assert CATCH_UP_POLICIES == ("skip", "once", "all-bounded")

    def test_skip_drops_missed_occurrence(self):
        job = self._caught_up_job(catch_up="skip")
        result = _fire(job)
        assert result[0] is True  # deliberate, not a failure
        conn = _connect()
        try:
            assert _tasks(conn, f"routine:{job['id']}:") == []
        finally:
            conn.close()
        assert "skip" in result[1]

    def test_once_creates_one_catch_up_card(self):
        job = self._caught_up_job(catch_up="once")
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            rows = _tasks(conn, f"routine:{job['id']}:")
            assert len(rows) == 1
            assert rows[0]["idempotency_key"] == f"routine:{job['id']}:{self.FIRED}"
        finally:
            conn.close()

    def test_default_policy_is_once(self):
        job = self._caught_up_job()  # no catch_up key
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            assert len(_tasks(conn, f"routine:{job['id']}:")) == 1
        finally:
            conn.close()

    def test_all_bounded_backfills_gap(self):
        # interval 60m: previous un-materialized slots are 11:00, 10:00, 09:00...
        job = self._caught_up_job(catch_up="all-bounded", catch_up_bound=3)
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            rows = _tasks(conn, f"routine:{job['id']}:")
            assert len(rows) == 3  # fired + 2 backfill, oldest first
            keys = {r["idempotency_key"] for r in rows}
            assert keys == {
                f"routine:{job['id']}:{self.FIRED}",
                "routine:routinejob:2026-09-28T11:00:00+00:00",
                "routine:routinejob:2026-09-28T10:00:00+00:00",
            }
        finally:
            conn.close()

    def test_all_bounded_never_floods(self):
        # 30m interval, a week of missed slots, bound 3 -> exactly 3 cards
        job = _job(
            instant=self.FIRED, dispatch_kind="catch_up",
            schedule={"kind": "interval", "minutes": 30},
            block=dict(BLOCK, catch_up="all-bounded", catch_up_bound=3))
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            assert len(_tasks(conn, f"routine:{job['id']}:")) == 3
        finally:
            conn.close()

    def test_all_bounded_stops_at_gap_boundary(self):
        from hermes_cli import kanban_db as kb

        boundary = "2026-09-28T10:00:00+00:00"  # two slots before the fired one
        conn = _connect()
        try:
            kb.create_task(conn, title="already materialized",
                           idempotency_key=f"routine:routinejob:{boundary}",
                           board="default")
        finally:
            conn.close()
        job = self._caught_up_job(catch_up="all-bounded", catch_up_bound=5)
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            rows = _tasks(conn, f"routine:{job['id']}:")
            # 1 boundary card (pre-existing) + 11:00 backfill + fired 12:00;
            # the walk stopped at the boundary instead of materializing 09:00
            assert {r["idempotency_key"] for r in rows} == {
                f"routine:{job['id']}:{self.FIRED}",
                "routine:routinejob:2026-09-28T11:00:00+00:00",
                "routine:routinejob:2026-09-28T10:00:00+00:00",
            }
        finally:
            conn.close()

    def test_policy_applies_only_to_catch_up_fires(self):
        # skip policy + ON-TIME fire still materializes the occurrence
        job = _job(instant=self.FIRED, dispatch_kind="on_time",
                   block=dict(BLOCK, catch_up="skip"))
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            assert len(_tasks(conn, f"routine:{job['id']}:")) == 1
        finally:
            conn.close()

    def test_occurrences_note_shape(self):
        job = _job(instant=self.FIRED, block=dict(BLOCK, catch_up="all-bounded",
                                                  catch_up_bound=2))
        occurrences, note = occurrences_to_materialize(
            job, job["kanban"], self.FIRED, "catch_up", lambda key: False)
        assert len(occurrences) == 2
        assert note["bound"] == 2 and note["backfilled"] == 1


# ---------------------------------------------------------------------------
# DST / UTC identity
# ---------------------------------------------------------------------------

class TestDstUtcIdentity:
    def test_same_instant_different_offsets_same_key(self):
        block = normalize_kanban_block(BLOCK)
        summer = canonical_instant("2026-10-24T09:00:00+02:00")
        utc_spelling = canonical_instant("2026-10-24T07:00:00+00:00")
        assert summer == utc_spelling  # canonicalization is the contract
        assert routine_idempotency_key(block, "j", summer) == (
            routine_idempotency_key(block, "j", utc_spelling))

    def test_prev_walk_across_dst_fall_back_is_distinct_and_wall_clock_stable(
            self, monkeypatch):
        import hermes_time

        monkeypatch.setattr(
            hermes_time, "get_timezone", lambda: ZoneInfo("Europe/Berlin"))
        # DST fall-back: 2026-10-25 03:00 CEST -> 02:00 CET
        fired_local = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        instants = previous_instants(
            {"kind": "cron", "expr": "0 9 * * *"}, fired_local.isoformat(), 4)
        assert len(instants) == 4
        utc_keys = [canonical_instant(i) for i in instants]
        assert len(set(utc_keys)) == 4  # pairwise distinct across the boundary
        zone = ZoneInfo("Europe/Berlin")
        for instant in instants:
            local = datetime.fromisoformat(instant).astimezone(zone)
            assert (local.hour, local.minute) == (9, 0)  # wall-clock intent holds
        # the straddling pair really carries different UTC offsets
        offsets = {datetime.fromisoformat(i).utcoffset() for i in instants}
        assert len(offsets) == 2

    def test_interval_prev_walk_is_utc_stable(self):
        fired = "2026-09-28T12:00:00+00:00"
        instants = previous_instants({"kind": "interval", "minutes": 60}, fired, 3)
        assert [canonical_instant(i) for i in instants] == [
            "2026-09-28T11:00:00+00:00",
            "2026-09-28T10:00:00+00:00",
            "2026-09-28T09:00:00+00:00",
        ]

    def test_unsupported_schedule_kinds_yield_no_backfill(self):
        assert previous_instants({"kind": "once", "run_at": "x"}, "2026-09-28T12:00:00+00:00", 3) == []
        assert previous_instants({}, "2026-09-28T12:00:00+00:00", 3) == []


# ---------------------------------------------------------------------------
# no_agent routines: deterministic scripts, cards only on exception
# ---------------------------------------------------------------------------

class TestNoAgentRoutines:
    INSTANT = "2026-09-28T09:00:00+00:00"

    def _script_job(self, script):
        return _job(instant=self.INSTANT, dispatch_kind="on_time", no_agent=True,
                    script=script, block=dict(BLOCK, assignee="worker-maintain"))

    def test_script_success_creates_no_card(self):
        script = _write_script("routine-ok.sh", "#!/usr/bin/env bash\necho all good\n")
        job = self._script_job(script)
        result = _fire(job)
        assert result[0] is True
        conn = _connect()
        try:
            assert _tasks(conn, "routine:") == []
        finally:
            conn.close()

    def test_script_failure_creates_exception_card_once(self):
        script = _write_script("routine-fail.sh", "#!/usr/bin/env bash\necho boom >&2\nexit 3\n")
        job = self._script_job(script)
        result = _fire(job)
        assert result[0] is False  # cron still records the script failure
        assert "exception card" in result[1]
        conn = _connect()
        try:
            rows = _tasks(conn, f"routine:{job['id']}:")
            assert len(rows) == 1
            from hermes_cli import kanban_db as kb

            task = kb.get_task(conn, rows[0]["id"])
            assert task.title.endswith("routine exception")
            assert "boom" in task.body
            assert task.assignee == "worker-maintain"
        finally:
            conn.close()
        # crash re-fire of the same occurrence: no second exception card
        again = _fire(job)
        assert again[0] is False
        conn = _connect()
        try:
            assert len(_tasks(conn, f"routine:{job['id']}:")) == 1
        finally:
            conn.close()

    def test_script_failure_on_skipped_catch_up_makes_no_card(self):
        script = _write_script("routine-fail-cu.sh", "#!/usr/bin/env bash\nexit 1\n")
        job = _job(instant=self.INSTANT, dispatch_kind="catch_up", no_agent=True,
                   script=script, block=dict(BLOCK, catch_up="skip"))
        result = _fire(job)
        assert result[0] is False  # the failure is still reported...
        conn = _connect()
        try:
            assert _tasks(conn, f"routine:{job['id']}:") == []  # ...but no card
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Legacy jobs unchanged
# ---------------------------------------------------------------------------

class TestLegacyJobsUnchanged:
    def test_no_block_job_skips_the_bridge(self, monkeypatch):
        import cron.kanban_routine as krn
        import cron.scheduler as sched

        def _must_not_run(*a, **k):  # pragma: no cover - fails the test if called
            raise AssertionError("bridge must not run for a job without a kanban block")

        monkeypatch.setattr(sched, "run_kanban_routine_job", _must_not_run)
        # an empty-payload job is blocked+paused by the legacy path — proving
        # the pre-agent gates behave exactly as before the feature.
        result, prompt = sched._prepare_job_prompt(
            {"id": "legacy", "name": "legacy", "prompt": "", "script": None,
             "no_agent": False, "kanban": None, "schedule": {"kind": "once"},
             "deliver": "local"}, "legacy", "legacy", None, None)
        assert result is not None
        assert result[0] is False

    def test_legacy_job_records_stay_byte_identical(self):
        from cron.jobs import create_job, job_payload_is_empty

        job = create_job(prompt="classic", schedule="every 2h")
        assert "kanban" not in job
        assert job_payload_is_empty({**job, "kanban": None}) is False
        # a kanban block counts as payload
        assert job_payload_is_empty({"prompt": "", "kanban": {"board": "b"}}) is False

    def test_scheduler_has_no_second_loop(self):
        # Spec acceptance 8-3: the delta introduces no new ticker. The bridge
        # is a pure per-fire function, invoked once per dispatched job.
        import inspect

        import cron.kanban_routine as krn

        source = inspect.getsource(krn)
        assert "while True" not in source


# ---------------------------------------------------------------------------
# CLI read/create paths
# ---------------------------------------------------------------------------

def _cli_args(**overrides):
    base = dict(
        schedule="0 9 * * *", prompt=None, skill=None, skills=None, no_agent=None,
        paused=False, paused_reason=None,
        # _JOB_ARG_FIELDS dests
        name=None, deliver=None, failure_deliver=None, repeat=None, script=None,
        workdir=None, model=None, model_provider=None, pinned=None,
        monitor_script=None, monitor_url=None, continuity=None, reasoning_effort=None,
        # kanban dests
        kanban_board=None, kanban_title=None, kanban_body_inline=None,
        kanban_body_file=None, kanban_assignee=None, kanban_priority=None,
        kanban_catch_up=None, kanban_catch_up_bound=None,
        kanban_workspace_kind=None, kanban_workspace_path=None,
        kanban_idempotency_key=None, kanban_clear=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCliPaths:
    def test_cli_create_and_list(self, capsys):
        from cron.jobs import list_jobs
        from hermes_cli import cron as cron_cli

        args = _cli_args(kanban_board="default", kanban_title="Weekly sweep",
                         kanban_assignee="worker-maintain",
                         kanban_catch_up="all-bounded", kanban_catch_up_bound=3)
        assert cron_cli.cron_create(args) == 0
        out = capsys.readouterr().out
        assert "Kanban routine: default — Weekly sweep" in out
        jobs = list_jobs(include_disabled=True)
        created = next(j for j in jobs if j.get("kanban"))
        assert created["kanban"]["catch_up"] == "all-bounded"
        # read path: cron list detail rows carry the routine line
        rows = dict(cron_cli._job_rows(created))
        assert rows["Kanban routine"] == (
            "default — Weekly sweep (catch-up: all-bounded, bound 3)")

    def test_cli_edit_merges_single_field(self):
        from cron.jobs import create_job, get_job
        from hermes_cli import cron as cron_cli

        job = create_job(prompt=None, schedule="0 9 * * *",
                         kanban={"board": "default", "title": "T1"})
        args = _cli_args(kanban_catch_up="skip")
        args.job_id = job["id"]
        assert cron_cli.cron_edit(args) == 0
        stored = get_job(job["id"])
        assert stored["kanban"]["title"] == "T1"  # merge, not replace
        assert stored["kanban"]["catch_up"] == "skip"

    def test_cli_edit_clear(self):
        from cron.jobs import create_job, get_job
        from hermes_cli import cron as cron_cli

        script = _write_script("cli-clear.sh", "#!/usr/bin/env bash\nexit 0\n")
        job = create_job(prompt=None, schedule="0 9 * * *", no_agent=True, script=script,
                         kanban={"board": "default", "title": "T1"})
        args = _cli_args(kanban_clear=True)
        args.job_id = job["id"]
        assert cron_cli.cron_edit(args) == 0
        assert "kanban" not in get_job(job["id"])

    def test_cli_create_without_kanban_flags_untouched(self):
        from cron.jobs import list_jobs
        from hermes_cli import cron as cron_cli

        before = {j["id"] for j in list_jobs(include_disabled=True)}
        assert cron_cli.cron_create(_cli_args(prompt="classic job")) == 0
        new = [j for j in list_jobs(include_disabled=True) if j["id"] not in before]
        assert len(new) == 1 and "kanban" not in new[0]


# ---------------------------------------------------------------------------
# Tool read/create paths
# ---------------------------------------------------------------------------

class TestToolPaths:
    @staticmethod
    def _tool(**kwargs):
        from tools.cronjob_tools import cronjob

        return json.loads(cronjob(**kwargs))

    def test_tool_create_and_list(self):
        result = self._tool(
            action="create", schedule="every 2h",
            kanban={"board": "default", "title": "Tool routine",
                    "assignee": "worker-maintain", "catch_up": "once"})
        assert result["success"], result
        assert result["job"]["kanban"]["title"] == "Tool routine"
        listing = self._tool(action="list", include_disabled=True)
        entry = next(j for j in listing["jobs"] if j["job_id"] == result["job_id"])
        assert entry["kanban"]["board"] == "default"

    def test_tool_create_requires_block_fields(self):
        result = self._tool(action="create", schedule="every 2h",
                           kanban={"board": "default"})
        assert result["success"] is False
        assert "title" in result["error"]

    def test_tool_update_replaces_and_clears(self):
        created = self._tool(
            action="create", schedule="every 2h",
            kanban={"board": "default", "title": "T1"})
        job_id = created["job_id"]
        updated = self._tool(action="update", job_id=job_id,
                             kanban={"board": "default", "title": "T2"})
        assert updated["job"]["kanban"]["title"] == "T2"
        # clearing the block of a block-only job refuses (empty payload) —
        # the job would have nothing to run
        cleared = self._tool(action="update", job_id=job_id, kanban={})
        assert cleared["success"] is False
        assert "nothing to run" in cleared["error"]

    def test_tool_create_workspace_round_trips(self):
        # Regression: the tool schema advertises a nested `workspace` object —
        # the stored block must carry it through to materialization (flattened
        # workspace_kind/workspace_path keys would be silently dropped by the
        # block normalizer, giving created tasks a scratch workspace).
        from cron.jobs import get_job

        created = self._tool(
            action="create", schedule="every 2h",
            kanban={"board": "default", "title": "WS routine",
                    "workspace": {"kind": "dir", "path": "/tmp"}})
        assert created["success"], created
        stored = get_job(created["job_id"])
        assert stored["kanban"]["workspace"] == {"kind": "dir", "path": "/tmp"}
        # the flattened aliases are NOT valid block keys — the stored block
        # never carries them
        assert "workspace_kind" not in stored["kanban"]
        assert "workspace_path" not in stored["kanban"]

    def test_tool_handler_forwards_kanban(self):
        from tools.cronjob_tools import _HANDLER_FORWARDED_ARGS

        assert "kanban" in _HANDLER_FORWARDED_ARGS