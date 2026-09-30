"""SessionDB.session_spend_totals — one session's usage for the kanban run-cost ledger.

Governance stage 1 (docs/kanban-governance-spec.md, Stufe 1): the worker exit
flush attributes a run's cost from its own ``session_model_usage`` rows.

The usage table stores amounts in ``NOT NULL DEFAULT 0`` columns (the writer
persists ``float(x or 0.0)``), so the honest price discriminator is each row's
``cost_status``: ``actual`` (known billed price), ``included`` (a known $0,
subscription-included), ``estimated`` (estimate only), NULL/``unknown`` (never
priced). The contract under test: the session status ladder (``actual`` only
when EVERY priced row is actual/included, ``estimated`` when a mix or estimates
only, ``unknown`` with NULL amounts — never 0 — when nothing was priced or the
session has no rows at all).
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", "cli")
    db.create_session("s2", "cli")
    yield db
    db.close()


def _insert_usage(db_path, session_id, model, *, estimated=0.0, actual=0.0,
                  status=None, input_tokens=0, output_tokens=0, calls=1):
    """Seed one usage row the way the turn loop's upsert would leave it:
    numeric amounts (NOT NULL columns) + the cost_status discriminator."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, api_call_count, "
            "input_tokens, output_tokens, estimated_cost_usd, actual_cost_usd, cost_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, model, calls, input_tokens, output_tokens,
             float(estimated or 0.0), float(actual or 0.0), status),
        )
        conn.commit()
    finally:
        conn.close()


def test_session_without_rows_returns_none(db, tmp_path):
    """Nothing measured -> None: the ledger records 'unknown', never 0."""
    assert db.session_spend_totals("s1") is None


def test_empty_session_id_returns_none(db):
    assert db.session_spend_totals("") is None


def test_estimated_only_rows_aggregate_as_estimated(db, tmp_path):
    _insert_usage(tmp_path / "state.db", "s1", "m1",
                  estimated=1.5, status="estimated", input_tokens=10, output_tokens=5)
    _insert_usage(tmp_path / "state.db", "s1", "m2",
                  estimated=2.5, status="estimated", input_tokens=100,
                  output_tokens=50, calls=3)
    totals = db.session_spend_totals("s1")
    assert totals["cost_status"] == "estimated"
    assert totals["estimated_cost_usd"] == pytest.approx(4.0)
    assert totals["actual_cost_usd"] is None
    assert totals["input_tokens"] == 110
    assert totals["output_tokens"] == 55
    assert totals["api_call_count"] == 4


def test_all_actual_rows_aggregate_as_actual(db, tmp_path):
    _insert_usage(tmp_path / "state.db", "s1", "m1",
                  estimated=1.0, actual=1.0, status="actual")
    _insert_usage(tmp_path / "state.db", "s1", "m2",
                  estimated=2.0, actual=2.0, status="actual")
    totals = db.session_spend_totals("s1")
    assert totals["cost_status"] == "actual"
    assert totals["actual_cost_usd"] == pytest.approx(3.0)
    assert totals["estimated_cost_usd"] is None


def test_included_rows_are_a_known_zero_price(db, tmp_path):
    """'included' = subscription-included: a KNOWN $0, not an unknown."""
    _insert_usage(tmp_path / "state.db", "s1", "m1",
                  estimated=0.0, actual=0.0, status="included", input_tokens=42)
    totals = db.session_spend_totals("s1")
    assert totals["cost_status"] == "actual"
    assert totals["actual_cost_usd"] == pytest.approx(0.0)
    assert totals["input_tokens"] == 42


def test_mixed_actual_and_estimated_downgrades_to_estimated(db, tmp_path):
    """One estimate-only row downgrades the whole session to 'estimated'."""
    _insert_usage(tmp_path / "state.db", "s1", "m1",
                  estimated=1.0, actual=1.0, status="actual")
    _insert_usage(tmp_path / "state.db", "s1", "m2",
                  estimated=2.0, status="estimated")
    totals = db.session_spend_totals("s1")
    assert totals["cost_status"] == "estimated"
    assert totals["estimated_cost_usd"] == pytest.approx(3.0)
    assert totals["actual_cost_usd"] is None


def test_unpriced_rows_are_unknown_with_null_amounts_never_zero(db, tmp_path):
    """Rows exist but nothing was priced -> 'unknown' with NULL amounts.

    The table stores 0.0 in the amount columns (NOT NULL) — the ledger must not
    reinterpret that stored zero as a measured price.
    """
    _insert_usage(tmp_path / "state.db", "s1", "m1", input_tokens=7)
    _insert_usage(tmp_path / "state.db", "s1", "m2",
                  estimated=0.0, status="unknown", input_tokens=3)
    totals = db.session_spend_totals("s1")
    assert totals["cost_status"] == "unknown"
    assert totals["estimated_cost_usd"] is None
    assert totals["actual_cost_usd"] is None
    # tokens are still measured and reported
    assert totals["input_tokens"] == 10


def test_other_sessions_do_not_leak_into_totals(db, tmp_path):
    _insert_usage(tmp_path / "state.db", "s1", "m1", estimated=1.0, status="estimated")
    _insert_usage(tmp_path / "state.db", "s2", "m1", estimated=99.0, status="estimated")
    assert db.session_spend_totals("s1")["estimated_cost_usd"] == pytest.approx(1.0)
