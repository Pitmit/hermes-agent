"""Governance operator views in the Kanban dashboard plugin (governance P1-B2).

Tests the /api/plugins/kanban/governance/* routes — thin wrappers over the
governance kernels — through the same bare-FastAPI harness the dashboard
plugin suite uses. Contracts under test:

* budget/cost status per scope with ok/warn/stopped states;
* approval list + decide with every kernel fence intact (drift forbids
  approve, self-approval refused, one decision per request — a double POST is
  a 409, not a second mutation);
* blocked inbox: deterministic severity order, fail-closed filters;
* project rollup + unknown-project refusal;
* watchdog/workflow status;
* running progress: bounded allowlist projection with secret redaction —
  worker pids, claim locks and run metadata never appear, unknown ETA stays
  ``null`` instead of being invented;
* the frontend bundle's governance helpers (severity sort, decision gating,
  double-click fence, ETA-unknown rendering) via the node probe fixture.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

GOV = "/api/plugins/kanban/governance"


# ---------------------------------------------------------------------------
# Fixtures (same harness as tests/plugins/test_kanban_dashboard_plugin.py)
# ---------------------------------------------------------------------------

def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_gov_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _board() -> str:
    return kb.get_current_board() or kb.DEFAULT_BOARD


def _seed_approval(client, *, title="needs a human", requester="worker-a"):
    """Task + open approval request bound to it (worker → human gate)."""
    from hermes_cli import kanban_approvals as kba

    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title=title, assignee="worker-a")
        ap = kba.request_approval(
            conn, board=_board(), type="action", subject_kind="task",
            subject_ref=task, requester=requester, note="please review",
        )
        assert kb.block_task(conn, task, reason=f"approval:{ap['id']}", kind="needs_input")
    return task, ap["id"]


# ---------------------------------------------------------------------------
# Empty-board shapes
# ---------------------------------------------------------------------------

def test_governance_views_empty_board(client):
    for path, key in (
        ("/approvals", "approvals"),
        ("/inbox", "rows"), ("/watchdogs", "watchdogs"),
        ("/progress", "runs"),
    ):
        r = client.get(GOV + path)
        assert r.status_code == 200, f"{path}: {r.text}"
        body = r.json()
        assert key in body, f"{path}: missing {key!r} in {body}"
        assert body[key] == [], f"{path}: expected empty list"
    # Workflows: init seeds the reference template set onto a fresh board, so
    # the contract is a list (seeded here), never an error.
    templates = client.get(GOV + "/workflows").json()["templates"]
    assert isinstance(templates, list)
    # budget carries the board-month usage block even with no budgets
    budget = client.get(GOV + "/budget").json()["budget"]
    assert budget["budgets"] == []
    assert budget["mtd_usd"] == 0.0
    assert budget["unknown_runs"] == 0


# ---------------------------------------------------------------------------
# Budget & costs
# ---------------------------------------------------------------------------

def test_budget_states_and_scopes(client):
    from hermes_cli import kanban_cost as kcost

    now = int(time.time())
    period = kcost.current_period()
    with kbc.connect_closing() as conn:
        # Two profiles with spend against their budgets: one ok, one stopped.
        kcost.set_budget(conn, board=_board(), scope="profile", ref="worker-a",
                         limit_usd=10.0, created_by="test")
        kcost.set_budget(conn, board=_board(), scope="profile", ref="worker-b",
                         limit_usd=5.0, created_by="test")
        for run_id, (profile, spend) in enumerate(
            (("worker-a", 3.0), ("worker-b", 6.0)), start=1,
        ):
            task = kb.create_task(conn, title=f"cost task {run_id}", assignee=profile)
            # runs_total counts task_runs started in the period — seed the runs
            # the ledger rows hang off.
            conn.execute(
                "INSERT INTO task_runs (task_id, status, profile, started_at, ended_at) "
                "VALUES (?, 'success', ?, ?, ?)",
                (task, profile, now, now + 60),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO task_run_costs (run_id, task_id, board, profile, period, "
                "estimated_cost_usd, cost_status, recorded_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'estimated', ?, ?)",
                (run_id, task, _board(), profile, period, spend, now, now),
            )
        # A tenant-scoped budget with no spend yet.
        kcost.set_budget(conn, board=_board(), scope="tenant", ref="acme",
                         limit_usd=100.0, created_by="test")
        conn.commit()

    body = client.get(GOV + "/budget").json()["budget"]
    assert body["period"] == period
    by_key = {(b["scope"], b["ref"]): b for b in body["budgets"]}
    assert by_key[("profile", "worker-a")]["state"] == "ok"
    assert by_key[("profile", "worker-a")]["mtd_usd"] == 3.0
    assert by_key[("profile", "worker-b")]["state"] == "stopped"
    assert by_key[("profile", "worker-b")]["mtd_usd"] == 6.0
    assert by_key[("tenant", "acme")]["state"] == "ok"
    assert body["mtd_usd"] == 9.0  # board-month known spend covers every scope
    assert body["runs_total"] >= 2


def test_budget_bad_period_is_400(client):
    r = client.get(GOV + "/budget?period=not-a-period")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Approvals — list, drift, decide fences
# ---------------------------------------------------------------------------

def test_approval_list_shows_pending_and_subject(client):
    task, ap_id = _seed_approval(client)
    rows = client.get(GOV + "/approvals").json()["approvals"]
    assert [a["id"] for a in rows] == [ap_id]
    row = rows[0]
    assert row["status"] == "pending"
    assert row["subject_kind"] == "task"
    assert row["subject_id"] == task
    assert row["requester"] == "worker-a"

    single = client.get(f"{GOV}/approvals/{ap_id}").json()["approval"]
    assert single["id"] == ap_id


def test_approval_drift_invalidates_on_read_and_forbids_decide(client):
    """A stale view can never approve a changed subject (governance contract)."""
    task, ap_id = _seed_approval(client)
    with kbc.connect_closing() as conn:
        kb.edit_task(conn, task, body="the subject changed under the request")

    # The list drift-checks on read: the request surfaces invalidated…
    rows = client.get(GOV + "/approvals").json()["approvals"]
    assert rows[0]["status"] == "invalidated"
    assert rows[0]["invalidation_reason"] == "subject_drift"

    # …and the decision is refused (409) — the task stays blocked.
    r = client.post(f"{GOV}/approvals/{ap_id}/decide",
                    json={"decision": "approve", "approver": "operator"})
    assert r.status_code == 409
    assert "invalidated" in r.json()["detail"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task).status == "blocked"


def test_approval_decide_approve_releases_bound_task(client):
    task, ap_id = _seed_approval(client)
    r = client.post(f"{GOV}/approvals/{ap_id}/decide",
                    json={"decision": "approve", "approver": "operator",
                          "note": "looks good"})
    assert r.status_code == 200, r.text
    body = r.json()["approval"]
    assert body["status"] == "approved"
    assert body["approver"] == "operator"
    assert body["decided_via"] == "dashboard"
    assert body["decision_note"] == "looks good"
    assert body["released_tasks"] == [task]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task).status != "blocked"


def test_approval_double_decide_is_409_not_a_second_mutation(client):
    """Doppelclick idempotent: the second POST is answered with the current
    state (409 already-decided), never a second decision or event."""
    _, ap_id = _seed_approval(client)
    first = client.post(f"{GOV}/approvals/{ap_id}/decide",
                        json={"decision": "approve", "approver": "operator"})
    assert first.status_code == 200
    second = client.post(f"{GOV}/approvals/{ap_id}/decide",
                         json={"decision": "approve", "approver": "operator"})
    assert second.status_code == 409
    assert "already decided" in second.json()["detail"]

    from hermes_cli import kanban_approvals as kba
    with kbc.connect_closing() as conn:
        events = kba.approval_events(conn, ap_id)
    decided = [e for e in events if e["kind"] == "approval_decided"]
    assert len(decided) == 1  # exactly one decision event, ever


def test_approval_self_approval_forbidden(client):
    _, ap_id = _seed_approval(client, requester="worker-a")
    r = client.post(f"{GOV}/approvals/{ap_id}/decide",
                    json={"decision": "approve", "approver": "worker-a"})
    assert r.status_code == 403
    assert "self-approval" in r.json()["detail"]


def test_approval_revise_and_reject_transitions(client):
    _, ap_id = _seed_approval(client)
    r = client.post(f"{GOV}/approvals/{ap_id}/decide",
                    json={"decision": "revise", "approver": "operator"})
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "revision_requested"

    # A revision request is still open: reject closes it.
    r = client.post(f"{GOV}/approvals/{ap_id}/decide",
                    json={"decision": "reject", "approver": "operator"})
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "rejected"


def test_approval_not_found_and_bad_filters(client):
    assert client.post(f"{GOV}/approvals/ap_deadbeef/decide",
                       json={"decision": "approve"}).status_code == 404
    assert client.get(f"{GOV}/approvals/ap_deadbeef").status_code == 404
    assert client.get(GOV + "/approvals?status=bogus").status_code == 400
    assert client.get(GOV + "/approvals?type=bogus").status_code == 400
    r = client.post(f"{GOV}/approvals/ap_whatever/decide", json={"decision": "nope"})
    assert r.status_code == 400  # invalid decision verb


def test_approval_decide_from_worker_context_is_refused(client, monkeypatch):
    """The decide route is a human surface: a dispatched-worker env never decides."""
    _, ap_id = _seed_approval(client)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_some_workertask")
    r = client.post(f"{GOV}/approvals/{ap_id}/decide",
                    json={"decision": "approve", "approver": "operator"})
    assert r.status_code == 403
    assert "workers cannot decide" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Blocked inbox
# ---------------------------------------------------------------------------

def _blocked_task_with_age(client, *, title, age_hours, sla_hours=None):
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title=title, assignee="worker-a")
        assert kb.block_task(conn, task, reason="waiting for input", kind="needs_input")
        if sla_hours is not None:
            conn.execute("UPDATE tasks SET block_sla_hours=? WHERE id=?",
                         (sla_hours, task))
        # Age the block: rewind the blocked event beyond the SLA bands.
        old = int(time.time()) - int(age_hours * 3600)
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='blocked'",
            (old, task),
        )
        conn.commit()
    return task


def test_inbox_severity_order_and_sources(client):
    low_task = _blocked_task_with_age(client, title="fresh block", age_hours=0.5)
    crit_task = _blocked_task_with_age(client, title="ancient block",
                                       age_hours=200)  # > 6x a 24h SLA → critical
    _, ap_id = _seed_approval(client)  # pending approval → source=approval

    body = client.get(GOV + "/inbox").json()
    rows = body["rows"]
    sevs = [r["severity"] for r in rows]
    # Deterministic total order: severity desc, then age desc, then ref.
    assert sevs == sorted(sevs, key=lambda s: -{"low": 0, "medium": 1, "high": 2, "critical": 3}[s])
    sources = {r["source"] for r in rows}
    assert sources == {"blocked", "approval"}
    by_ref = {r["ref"]: r for r in rows}
    assert by_ref[crit_task]["severity"] == "critical"
    assert by_ref[low_task]["severity"] in ("low", "medium")
    assert by_ref[ap_id]["source"] == "approval"
    assert by_ref[ap_id]["action_owner"] == "human-approver"
    # SLA is surfaced per row (hours + whether an override applies).
    assert by_ref[crit_task]["sla_hours"] > 0
    assert "age_seconds" in by_ref[crit_task]

    # Server-side severity filter is fail-closed and inclusive.
    only_crit = client.get(GOV + "/inbox?severity=critical").json()["rows"]
    assert {r["severity"] for r in only_crit} == {"critical"}
    assert client.get(GOV + "/inbox?severity=bogus").status_code == 400
    assert client.get(GOV + "/inbox?source=bogus").status_code == 400
    assert client.get(GOV + "/inbox?block_kind=bogus").status_code == 400
    # limit below 1 is refused (kernel ValueError->400, or FastAPI's own 422
    # constraint guard — both fail closed, never a silent full read).
    assert client.get(GOV + "/inbox?limit=0").status_code in (400, 422)


def test_inbox_review_source(client):
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="in review", assignee="worker-a")
        conn.execute(
            "UPDATE tasks SET status='review', started_at=? WHERE id=?",
            (int(time.time()) - 7200, task),
        )
        conn.commit()
    rows = client.get(GOV + "/inbox?source=review").json()["rows"]
    assert [r["task_id"] for r in rows] == [task]
    assert rows[0]["action_owner"] == "reviewer"


# ---------------------------------------------------------------------------
# Project rollup
# ---------------------------------------------------------------------------

def test_project_rollup(client):
    from hermes_cli import kanban_projects as kproj

    project_id = "prj_test_rollup"
    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="done thing", assignee="worker-a")
        b = kb.create_task(conn, title="open thing", assignee="worker-a")
        # Board-side project footprint (create_task drops a project link that is
        # unknown to this profile's registry — the validator's board path reads
        # tasks.project_id directly, so anchor it here).
        conn.execute("UPDATE tasks SET project_id=? WHERE id IN (?, ?)",
                     (project_id, a, b))
        kb.complete_task(conn, a, result="finished")
        kb.block_task(conn, b, reason="waiting", kind="needs_input")
        kproj.set_project_goal(
            conn, board=_board(), project_id=project_id,
            goal="Ship the governance dashboard", owner="operator",
            monthly_budget_usd=250.0, created_by="test",
        )
        conn.commit()

    r = client.get(f"{GOV}/projects/rollup?project_id={project_id}")
    assert r.status_code == 200, r.text
    rollup = r.json()["rollup"]
    assert rollup["project_id"] == project_id
    assert rollup["tasks"]["total"] == 2
    assert rollup["tasks"]["blocked"] == 1
    assert rollup["progress"]["done"] == 1
    assert 0 < rollup["progress"]["done_ratio"] <= 1
    assert rollup["goal"]["owner"] == "operator"
    assert rollup["goal"]["monthly_budget_usd"] == 250.0
    assert rollup["costs"]["period"]  # current YYYY-MM


def test_project_rollup_unknown_refused(client):
    r = client.get(GOV + "/projects/rollup?project_id=does-not-exist")
    assert r.status_code == 400
    assert "unknown project reference" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Watchdogs & workflows
# ---------------------------------------------------------------------------

def test_watchdogs_view(client):
    from hermes_cli import kanban_watchdog as kbwd

    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="watched", assignee="worker-a")
        wd = kbwd.create_watchdog(
            conn, board=_board(), task_id=task, reviewer="reviewer-b",
            instructions="verify the diff", created_by="operator",
        )

    body = client.get(GOV + "/watchdogs").json()
    assert body["watchdogs"] == [wd]
    assert body["watchdogs"][0]["task_id"] == task
    assert body["watchdogs"][0]["reviewer"] == "reviewer-b"
    assert body["firings"] == []
    assert client.get(GOV + "/watchdogs?status=bogus").status_code == 400
    active = client.get(GOV + "/watchdogs?status=active").json()["watchdogs"]
    assert [w["id"] for w in active] == [wd["id"]]


def test_workflows_view_lists_reference_templates(client):
    from hermes_cli import kanban_workflows as kbwf

    with kbc.connect_closing() as conn:
        created = kbwf.ensure_reference_templates(conn, board=None)
    assert created >= 1

    templates = client.get(GOV + "/workflows").json()["templates"]
    assert templates, "reference templates must be listed"
    tpl = templates[0]
    assert tpl["id"] and tpl["name"]
    assert isinstance(tpl["steps"], list) and tpl["steps"]
    step = tpl["steps"][0]
    assert {"step_key", "title", "assignee"} <= set(step)
    assert step["assignee"]  # a ROLE, not a concrete profile


# ---------------------------------------------------------------------------
# Running progress — allowlist + secret redaction + ETA honesty
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYS = {
    "worker_pid", "claim_lock", "claim_expires", "cmdline", "metadata",
    "body", "result", "environment", "env", "log", "summary",
}


def _walk_keys(node, out):
    if isinstance(node, dict):
        out.update(node.keys())
        for v in node.values():
            _walk_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_keys(v, out)


def _seed_running_task(client, *, progress=None, secret_phase=False):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="long runner", assignee="worker-a")
        lock = f"lock-gov-{task}"
        conn.execute(
            "UPDATE tasks SET status='running', started_at=?, claim_lock=?, "
            "claim_expires=?, worker_pid=?, body='body with internal notes', "
            "result='secret result text' WHERE id=?",
            (now - 600, lock, now + 3600, 424242, task),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, profile, started_at, metadata) "
            "VALUES (?, 'running', ?, ?, ?, 'worker-a', ?, ?)",
            (task, lock, now + 3600, 424242, now - 600,
             json.dumps({"internal": {"api_key": "sk-abcdef1234567890ZZ"}})),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task))
        if progress is not None:
            phase = progress.get("phase", "encoding")
            if secret_phase:
                phase = "phase with key sk-abcdef1234567890ZZ inside"
            conn.execute(
                "UPDATE task_runs SET progress_phase=?, progress_unit=?, "
                "progress_completed=?, progress_total=?, progress_rate=?, "
                "progress_eta_seconds=?, progress_error_count=?, progress_pct=?, "
                "progress_updated_at=? WHERE id=?",
                (phase, progress.get("unit", "files"),
                 progress.get("completed", 3), progress.get("total", 9),
                 progress.get("rate", 12.0), progress.get("eta_seconds"),
                 progress.get("error_count", 0), progress.get("pct", 33),
                 now - 30, run_id),
            )
        conn.commit()
    return task


def test_progress_view_redacts_secrets_and_leaks_no_process_state(client):
    _seed_running_task(client, progress={"eta_seconds": None}, secret_phase=True)

    body = client.get(GOV + "/progress").json()
    assert body["runs"], "running task with a live run must project"
    run = body["runs"][0]["run"]

    # Allowlist: none of the process/claim/content fields may appear anywhere.
    keys = set()
    _walk_keys(body, keys)
    leaked = keys & _FORBIDDEN_KEYS
    assert not leaked, f"governance progress leaked forbidden fields: {leaked}"

    # The heartbeat phase is secret-redacted: the raw key never appears.
    phase = run["phase"]
    assert phase is not None
    assert "sk-abcdef1234567890ZZ" not in json.dumps(body)

    # Unknown values stay unknown — never invented (ETA honesty contract).
    assert run["eta_seconds"] is None
    assert run["progress_pct"] == 33
    assert run["completed"] == 3 and run["total"] == 9
    assert run["unit"] == "files"


def test_progress_view_carries_known_eta_and_rate(client):
    _seed_running_task(client, progress={
        "eta_seconds": 1500, "rate": 12.0, "pct": 40, "error_count": 2,
    })
    run = client.get(GOV + "/progress").json()["runs"][0]["run"]
    assert run["eta_seconds"] == 1500
    assert run["rate"] == 12.0
    assert run["error_count"] == 2
    assert run["progress_pct"] == 40


def test_progress_view_excludes_finished_and_unstarted(client):
    with kbc.connect_closing() as conn:
        done = kb.create_task(conn, title="already done", assignee="worker-a")
        kb.complete_task(conn, done, result="ok")
        kb.create_task(conn, title="never started", assignee="worker-a")
        conn.commit()
    runs = client.get(GOV + "/progress").json()["runs"]
    assert runs == []


# ---------------------------------------------------------------------------
# Frontend bundle helpers — behavioral node probe (no build step: the bundle
# IS the source, so the probe extracts the shipped functions and drives them).
# ---------------------------------------------------------------------------

def test_governance_bundle_helpers_probe():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_governance_probe.js"
    result = subprocess.run(
        [node, str(probe), str(bundle)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout
