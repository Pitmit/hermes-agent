"""Structured progress heartbeats (governance P1-B1).

Contracts under test:
  - `kanban_heartbeat`'s structured fields (phase/completed/total/unit/rate/
    eta_seconds/error_count) persist additively on `task_runs` and in the
    existing `heartbeat` event — old note-only heartbeats stay byte-identical.
  - Validation is fail-closed: negative/NaN/infinite/boolean/non-integral/
    inconsistent values raise BEFORE anything is written.
  - Percent is derived only when `total > 0`; an absent ETA stays unknown
    (never invented from rate).
  - The activity projection carries bounded, secret-redacted fields and falls
    back to the pre-P1-B1 column set on legacy DBs the read-only poller never
    migrates (no bodies/env/PIDs/secrets ever enter the projection).
  - `hermes kanban show --json` exposes the same run fields the tool wrote.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time

import pytest

from hermes_cli import kanban_activity
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_output import _SHOW_RUN_FIELDS, _obj_dict


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return db_path


def _running_task(conn):
    tid = kb.create_task(conn, title="long op", assignee="worker")
    kb.claim_task(conn, tid)
    run_id = kb._current_run_id(conn, tid)
    return tid, run_id


def _run_progress_row(conn, run_id):
    return conn.execute(
        "SELECT progress_phase, progress_unit, progress_completed, progress_total, "
        "       progress_rate, progress_eta_seconds, progress_error_count, "
        "       progress_pct, progress_updated_at "
        "FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()


def _heartbeat_events(conn, tid):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'heartbeat' "
        "ORDER BY id ASC", (tid,)
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


def test_structured_heartbeat_sets_columns_and_event(isolated_board):
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        assert kbd.heartbeat_worker(
            conn, tid, note="halfway",
            progress={"phase": "encode", "completed": 4, "total": 8,
                      "unit": "files", "rate": 0.5, "eta_seconds": 300,
                      "error_count": 1},
        ) is True
        row = _run_progress_row(conn, run_id)
        assert (row["progress_phase"], row["progress_unit"]) == ("encode", "files")
        assert (row["progress_completed"], row["progress_total"]) == (4, 8)
        assert row["progress_rate"] == 0.5
        assert (row["progress_eta_seconds"], row["progress_error_count"]) == (300, 1)
        assert row["progress_pct"] == 50
        assert row["progress_updated_at"] is not None
        events = _heartbeat_events(conn, tid)
        assert len(events) == 1
        assert events[0]["note"] == "halfway"
        assert events[0]["progress_pct"] == 50
        assert events[0]["phase"] == "encode"
        assert events[0]["completed"] == 4 and events[0]["total"] == 8


def test_percent_only_when_total_positive(isolated_board):
    with connect_closing() as conn:
        # completed without total: count is valid, percent is NOT derived
        tid, run_id = _running_task(conn)
        assert kbd.heartbeat_worker(conn, tid, progress={"completed": 4}) is True
        row = _run_progress_row(conn, run_id)
        assert row["progress_completed"] == 4
        assert row["progress_total"] is None and row["progress_pct"] is None
        # total 0 (nothing to do yet) with completed 0: still no percent
        tid2, run_id2 = _running_task(conn)
        assert kbd.heartbeat_worker(
            conn, tid2, progress={"completed": 0, "total": 0}) is True
        row2 = _run_progress_row(conn, run_id2)
        assert row2["progress_completed"] == 0 and row2["progress_total"] == 0
        assert row2["progress_pct"] is None


def test_eta_unknown_is_never_invented(isolated_board):
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        # rate given, eta omitted: eta stays unknown — not derived from rate
        assert kbd.heartbeat_worker(conn, tid, progress={"rate": 2.0}) is True
        row = _run_progress_row(conn, run_id)
        assert row["progress_rate"] == 2.0
        assert row["progress_eta_seconds"] is None
        event = _heartbeat_events(conn, tid)[0]
        assert "eta_seconds" not in event
        assert "progress_pct" not in event


def test_note_only_heartbeat_stays_byte_identical(isolated_board):
    """Old heartbeats (pre-P1-B1) render unchanged: payload {"note": ...} / None,
    no structured columns touched."""
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        assert kbd.heartbeat_worker(conn, tid, note="still alive") is True
        assert _heartbeat_events(conn, tid) == [{"note": "still alive"}]
        assert kbd.heartbeat_worker(conn, tid) is True
        assert _heartbeat_events(conn, tid)[1] is None
        row = _run_progress_row(conn, run_id)
        assert all(value is None for value in tuple(row))


def test_auto_heartbeat_path_sets_no_structured_fields(isolated_board, monkeypatch):
    """The runtime-activity bridge calls heartbeat_worker with note=None only —
    the same call shape the CLI uses; it must not fabricate progress."""
    from tools import kanban_tools as kt
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert kt.heartbeat_current_worker_from_env() is True
    with connect_closing() as conn:
        row = _run_progress_row(conn, run_id)
        assert all(value is None for value in tuple(row))
        assert _heartbeat_events(conn, tid) == [None]


@pytest.mark.parametrize("progress", [
    {"completed": -1},
    {"total": -3},
    {"eta_seconds": -5},
    {"error_count": -2},
    {"rate": -0.5},
    {"completed": 5, "total": 3},           # completed > total
    {"completed": 2, "total": 0},           # completed > 0 with total == 0
    {"completed": float("nan")},
    {"total": float("inf")},
    {"eta_seconds": float("nan")},
    {"rate": float("nan")},
    {"completed": True},                    # bool is not a count
    {"completed": "4"},                     # string is not a number
    {"eta_seconds": 4.5},                   # fractional seconds
    {"phase": 123},                         # phase must be text
    {"phase": "   "},                       # blank after collapse
    {"unit": ""},                          # explicit empty is rejected
    {"pct": 50},                           # unknown field
])
def test_invalid_progress_fails_closed_before_any_write(isolated_board, progress):
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()["claim_expires"]
        with pytest.raises(ValueError):
            kbd.heartbeat_worker(conn, tid, progress=progress)
        # fail-closed: no column, no event, no liveness touch, no claim change
        assert all(value is None for value in tuple(_run_progress_row(conn, run_id)))
        assert _heartbeat_events(conn, tid) == []
        assert conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["last_heartbeat_at"] is None
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (tid,)).fetchone()["claim_expires"]
        assert after == before


def test_integral_floats_accepted_and_normalized(isolated_board):
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        assert kbd.heartbeat_worker(
            conn, tid, progress={"completed": 4.0, "total": 8.0, "eta_seconds": 300.0}
        ) is True
        row = _run_progress_row(conn, run_id)
        assert (row["progress_completed"], row["progress_total"]) == (4, 8)
        assert row["progress_eta_seconds"] == 300
        assert row["progress_pct"] == 50


def test_phase_and_unit_are_bounded_single_line(isolated_board):
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        assert kbd.heartbeat_worker(
            conn, tid, progress={"phase": "a  \n b" + "x" * 300, "unit": "f" * 100}
        ) is True
        row = _run_progress_row(conn, run_id)
        assert "\n" not in row["progress_phase"]
        assert len(row["progress_phase"]) <= 120
        assert len(row["progress_unit"]) <= 40


def test_non_monotone_progress_is_allowed(isolated_board):
    """Estimates may get worse; every change is visible in the event log."""
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        assert kbd.heartbeat_worker(conn, tid, progress={"completed": 6, "total": 8}) is True
        assert kbd.heartbeat_worker(conn, tid, progress={"completed": 3, "total": 8}) is True
        events = _heartbeat_events(conn, tid)
        assert [e["progress_pct"] for e in events] == [75, 38]


def test_show_json_run_fields_match_the_tool_call(isolated_board):
    """`hermes kanban show --json` emits exactly what the heartbeat wrote
    (_cmd_show serialises runs through _SHOW_RUN_FIELDS + _obj_dict)."""
    with connect_closing() as conn:
        tid, _ = _running_task(conn)
        assert kbd.heartbeat_worker(
            conn, tid, progress={"phase": "verify", "completed": 9, "total": 10,
                                 "unit": "tests", "eta_seconds": 60, "error_count": 0},
        ) is True
        run = kb.latest_run(conn, tid)
        assert (run.progress_phase, run.progress_completed, run.progress_total) == ("verify", 9, 10)
        assert run.progress_pct == 90
        assert run.progress_eta_seconds == 60
        emitted = _obj_dict(run, _SHOW_RUN_FIELDS)
        assert emitted["progress_pct"] == 90 and emitted["progress_eta_seconds"] == 60
        assert emitted["progress_phase"] == "verify" and emitted["progress_unit"] == "tests"


def _drop_progress_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        for col in ("progress_phase", "progress_unit", "progress_completed",
                    "progress_total", "progress_rate", "progress_eta_seconds",
                    "progress_error_count", "progress_pct", "progress_updated_at"):
            conn.execute(f"ALTER TABLE task_runs DROP COLUMN {col}")
        conn.commit()
    finally:
        conn.close()


def test_legacy_db_is_migrated_additively(isolated_board):
    """A pre-P1-B1 board gains the progress columns on the next writer
    connect; existing rows read back as unknown (None), never fabricated."""
    _drop_progress_columns(isolated_board)
    kb._INITIALIZED_PATHS.clear()  # a fresh process migrates on first connect
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        row = _run_progress_row(conn, run_id)
        assert all(value is None for value in tuple(row))
        assert kbd.heartbeat_worker(conn, tid, progress={"completed": 1, "total": 2}) is True
        assert _run_progress_row(conn, run_id)["progress_pct"] == 50


def test_activity_projection_carries_progress(isolated_board):
    with connect_closing() as conn:
        tid, _ = _running_task(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        assert kbd.heartbeat_worker(
            conn, tid, progress={"phase": "encode", "completed": 4, "total": 8,
                                 "unit": "files", "eta_seconds": 300, "error_count": 2},
        ) is True
    run = kb.get_activity_snapshot(board="default")["roots"][0]["run"]
    assert run["phase"] == "encode" and run["unit"] == "files"
    assert (run["completed"], run["total"]) == (4, 8)
    assert run["progress_pct"] == 50 and run["eta_seconds"] == 300
    assert run["error_count"] == 2
    assert run["progress_updated_at"] is not None


def test_activity_projection_redacts_and_bounds_progress_text(isolated_board):
    """phase/unit are model-supplied free text: the WS projection must be
    secret-safe and bounded even if the stored value is not."""
    secret = "sk-abc123def456ghi789"
    with connect_closing() as conn:
        tid, _ = _running_task(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.execute(
            "UPDATE task_runs SET progress_phase=?, progress_unit=? "
            "WHERE id = (SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1)",
            (f"token {secret}", "u" * 300, tid),
        )
        conn.commit()
    run = kb.get_activity_snapshot(board="default")["roots"][0]["run"]
    assert secret not in (run["phase"] or "")
    assert len(run["unit"]) <= kanban_activity.ACTIVITY_UNIT_MAX_CHARS


def test_activity_projection_stays_allowlisted(isolated_board):
    """The projection stays an allowlist: no bodies, env, PIDs, or raw secrets
    ride along with the new progress fields."""
    with connect_closing() as conn:
        tid, run_id = _running_task(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET worker_pid=?, summary=?, error=? WHERE id=?",
                     (4711, "summary body", "err body", run_id))
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES "
            "(?, 'heartbeat', ?, ?)",
            (tid, json.dumps({"note": "n", "phase": "p", "env": {"K": "V"}}), int(time.time())))
        conn.commit()
    node = kb.get_activity_snapshot(board="default")["roots"][0]
    run = node["run"]
    assert "worker_pid" not in run and "summary" not in run and "error" not in run
    assert "body" not in node and "env" not in run and "payload" not in run


def test_activity_projection_falls_back_on_legacy_columns(isolated_board):
    """The read-only poller never migrates; on a board whose task_runs table
    predates P1-B1 the snapshot still works and progress reads as unknown."""
    with connect_closing() as conn:
        tid, _ = _running_task(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.execute(
            "UPDATE task_runs SET last_heartbeat_at=? "
            "WHERE id = (SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1)",
            (int(time.time()), tid))
        conn.commit()
    _drop_progress_columns(isolated_board)
    snapshot = kb.get_activity_snapshot(board="default")
    run = snapshot["roots"][0]["run"]
    assert run["phase"] is None and run["progress_pct"] is None
    assert run["eta_seconds"] is None and run["completed"] is None
    assert run["last_heartbeat_at"] is not None  # the old fields still work


def test_legacy_heartbeat_events_still_render(isolated_board):
    """Old heartbeats written by pre-P1-B1 workers stay parseable/renderable
    in the projection's event consumer paths (payload None or {"note"})."""
    with connect_closing() as conn:
        tid, _ = _running_task(conn)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES "
            "(?, 'heartbeat', ?, ?)", (tid, json.dumps({"note": "legacy note"}), int(time.time())))
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES "
            "(?, 'heartbeat', NULL, ?)", (tid, int(time.time())))
        conn.commit()
    run = kb.get_activity_snapshot(board="default")["roots"][0]["run"]
    assert run["phase"] is None  # old heartbeats carry no structured fields
    assert run["progress_updated_at"] is None
