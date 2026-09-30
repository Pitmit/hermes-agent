"""Kanban workflow templates (governance stage 6, P1-A3).

Spec: docs/kanban-governance-spec.md §8. These tests prove the card's
contract for ``t_023152dd``:

* the five reference templates ship as DATA (seeded per board, idempotent,
  linear chains — a repeated step_key is the only cycle a linear list can
  express and is rejected);
* applying a template creates the EXACT graph and roles: one card per step,
  chained via task_links, every card stamped with workflow_template_id +
  current_step_key, first card ready, later cards parent-gated todo;
* the application validates profiles / forced skills / remote workspaces
  fail-closed BEFORE anything is persisted — an impossible application is
  rejected without a partial graph;
* a double application (lost-response retry, same idempotency key) is
  deduplicated: same chain, exactly one workflow_applied audit event;
* plain creates stay unchanged (no workflow fields, no workflow events) and
  a legacy board picks the table + reference set up on its next connect;
* the dormant plumbing is live end to end: claiming a step card stamps
  task_runs.step_key from tasks.current_step_key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_workflows as kwf
from hermes_cli.kanban_workflows import WorkflowTemplateError


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The test process may itself be a dispatched kanban worker — strip its
    # board/task env so board resolution stays deterministic.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
                "HERMES_TENANT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _make_profile(tmp_path, name, *, ssh=False, skills=()):
    """A real profile dir under the isolated home (Path.home patched)."""
    profile = tmp_path / ".hermes" / "profiles" / name
    (profile / "skills" / "local").mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n" if ssh else "{}\n", encoding="utf-8")
    for skill in skills:
        sdir = profile / "skills" / "local" / skill
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: {skill} here\n---\n# {skill}\n",
            encoding="utf-8")
    return profile


@pytest.fixture
def profiles(tmp_path, kanban_home):
    """peer (local backend, skills: available+local) and peer2 (plain)."""
    _make_profile(tmp_path, "peer", skills=("available", "local"))
    _make_profile(tmp_path, "peer2")
    return ("peer", "peer2")


# The five reference templates: id -> expected step keys (exact graphs).
REFERENCE = {
    "wf_research-synthesis-review": ["research", "synthesis", "review"],
    "wf_recon-implement-review": ["recon", "implement", "review"],
    "wf_implement-publish-gate-approve-merge": ["implement", "publish_gate", "approve", "merge"],
    "wf_incident-rca-repair-verify": ["incident", "rca", "repair", "verify"],
    "wf_migration-recon-snapshot-execute-userpath-accept": [
        "migration", "recon", "snapshot", "execute", "userpath", "accept"],
}


def _connect():
    return kbc.connect()


def _role_map_for(template, flip="peer2"):
    """Map every role the template uses, deterministically (peer/peer2)."""
    return {
        step.assignee: ("peer" if i % 2 == 0 else flip)
        for i, step in enumerate(template.steps)
    }


def _tasks(conn):
    return kb.list_tasks(conn, include_archived=True)


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _children(conn, tid):
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        (tid,)).fetchall()
    return [r["child_id"] for r in rows]


# ---------------------------------------------------------------- seeding


def test_reference_templates_seeded_linear_and_parseable(kanban_home):
    with kbc.connect_closing() as conn:
        templates = {t.id: t for t in kwf.list_templates(conn, board=None)}
    for tid, step_keys in REFERENCE.items():
        template = templates[tid]
        assert [s.step_key for s in template.steps] == step_keys
        # Linear chain: no step_key repeats (cycle/self-reference guard).
        assert len(set(step_keys)) == len(step_keys)
        assert all(s.assignee for s in template.steps), "every step names a role"


def test_reference_templates_seed_is_idempotent(kanban_home):
    with kbc.connect_closing() as conn:
        assert kwf.ensure_reference_templates(conn) == len(REFERENCE)
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM kanban_workflow_templates").fetchone()["n"]
    assert rows == len(REFERENCE)
    # Re-seeding (fresh process on the same board) changes nothing.
    with kbc.connect_closing() as conn:
        kwf.ensure_reference_templates(conn)
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM kanban_workflow_templates").fetchone()["n"]
    assert rows == len(REFERENCE)


def test_legacy_board_gains_templates_on_next_connect(kanban_home):
    with kbc.connect_closing() as conn:
        conn.execute("DROP TABLE kanban_workflow_templates")
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        templates = {t.id for t in kwf.list_templates(conn, board=None)}
    assert set(REFERENCE) <= templates


# ---------------------------------------------------------------- application


@pytest.mark.parametrize("tid,step_keys", sorted(REFERENCE.items()))
def test_apply_creates_exact_graph_and_roles(kanban_home, profiles, tid, step_keys):
    with kbc.connect_closing() as conn:
        template = kwf.get_template(conn, board=None, template_id=tid)
        roles = _role_map_for(template)
        applied = kwf.apply_workflow_template(
            conn, template_id=tid, title="Instance A",
            role_map=roles, created_by="tester")

        assert applied.template_id == tid
        assert applied.deduplicated is False
        assert len(applied.step_task_ids) == len(step_keys)
        assert applied.step_keys == tuple(step_keys)

        tasks = {t.current_step_key: t for t in _tasks(conn)}
        for key in step_keys:
            assert key in tasks, f"missing card for step {key}"
            task = tasks[key]
            assert task.workflow_template_id == tid
            assert task.status == ("ready" if key == step_keys[0] else "todo")
            assert task.assignee == roles[
                next(s.assignee for s in template.steps if s.step_key == key)]
            assert task.title.startswith("Instance A — ")

        # Exact chain: step k+1 is parent-gated on step k, nothing else.
        for i in range(len(step_keys) - 1):
            parent = tasks[step_keys[i]]
            child = tasks[step_keys[i + 1]]
            assert _children(conn, parent.id) == [child.id]

        # Gated cards record why (dependency_wait), root does not.
        assert not _events(conn, tasks[step_keys[0]].id, "dependency_wait")
        for key in step_keys[1:]:
            assert _events(conn, tasks[key].id, "dependency_wait")

        # Review / approval metadata lands on the step card bodies.
        for step in template.steps:
            body = tasks[step.step_key].body or ""
            if step.review:
                assert "REVIEW GATE" in body
            if step.approval_type:
                assert "APPROVAL GATE" in body and step.approval_type in body

        # Audit: exactly one workflow_applied event on the root card,
        # carrying the resolved graph and role map.
        events = _events(conn, applied.root_task_id, "workflow_applied")
        assert len(events) == 1
        payload = events[0].payload
        assert payload["template_id"] == tid
        assert [s["step_key"] for s in payload["steps"]] == step_keys
        assert payload["roles"] == roles


def test_claim_stamps_run_step_key_from_current_step_key(kanban_home, profiles):
    """The dormant plumbing is live end to end: task_runs.step_key is written
    from tasks.current_step_key when a workflow card is claimed."""
    tid = "wf_recon-implement-review"
    with kbc.connect_closing() as conn:
        template = kwf.get_template(conn, board=None, template_id=tid)
        applied = kwf.apply_workflow_template(
            conn, template_id=tid, title="Plumbing probe",
            role_map=_role_map_for(template))
        kb.claim_task(conn, applied.root_task_id)
        run = kb.latest_run(conn, applied.root_task_id)
    assert run.step_key == "recon"


def test_apply_unknown_template_rejected(kanban_home, profiles):
    with kbc.connect_closing() as conn:
        before = len(_tasks(conn))
        with pytest.raises(ValueError, match="does not exist"):
            kwf.apply_workflow_template(
                conn, template_id="wf_missing", title="X",
                role_map={"researcher": "peer"})
        assert len(_tasks(conn)) == before


def test_apply_missing_role_rejected_without_partial_graph(kanban_home, profiles):
    tid = "wf_recon-implement-review"
    with kbc.connect_closing() as conn:
        before = len(_tasks(conn))
        with pytest.raises(ValueError, match="no profile is mapped"):
            kwf.apply_workflow_template(
                conn, template_id=tid, title="X",
                role_map={"recon": "peer"})  # implementer/reviewer unmapped
        assert len(_tasks(conn)) == before
        assert all(t.workflow_template_id is None for t in _tasks(conn))


def test_apply_unknown_profile_rejected_without_partial_graph(kanban_home, tmp_path, profiles):
    _make_profile(tmp_path, "real")  # proves 'ghost' is the failing one
    tid = "wf_recon-implement-review"
    with kbc.connect_closing() as conn:
        before = len(_tasks(conn))
        with pytest.raises(ValueError, match="ghost.*is not installed"):
            kwf.apply_workflow_template(
                conn, template_id=tid, title="X",
                role_map={"recon": "ghost", "implementer": "real",
                          "reviewer": "real"})
        assert len(_tasks(conn)) == before
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE kind = 'workflow_applied'").fetchall()


def test_apply_forced_skill_rejected_without_partial_graph(kanban_home, tmp_path, profiles):
    _make_profile(tmp_path, "skilled", skills=("available",))
    steps = [
        {"step_key": "work", "title": "Work", "assignee": "worker",
         "skills": ["available", "missing-on-skilled"]},
        {"step_key": "check", "title": "Check", "assignee": "checker"},
    ]
    with kbc.connect_closing() as conn:
        kwf.create_template(conn, board=None, name="skill gate",
                            steps=steps, template_id="wf_skill-gate")
        before = len(_tasks(conn))
        with pytest.raises(ValueError, match="missing-on-skilled.*not available"):
            kwf.apply_workflow_template(
                conn, template_id="wf_skill-gate", title="X",
                role_map={"worker": "skilled", "checker": "peer"})
        assert len(_tasks(conn)) == before


def test_apply_remote_workspace_rejected_without_partial_graph(kanban_home, tmp_path, profiles):
    _make_profile(tmp_path, "sshpeer", ssh=True)
    host_dir = tmp_path / "ws"  # host-local, NOT under the board's shared root
    host_dir.mkdir()
    steps = [
        {"step_key": "work", "title": "Work", "assignee": "worker",
         "workspace_kind": "dir", "workspace_path": str(host_dir)},
        {"step_key": "check", "title": "Check", "assignee": "checker"},
    ]
    with kbc.connect_closing() as conn:
        kwf.create_template(conn, board=None, name="ws gate",
                            steps=steps, template_id="wf_ws-gate")
        before = len(_tasks(conn))
        with pytest.raises(ValueError, match="outside the board's shared workspaces root"):
            kwf.apply_workflow_template(
                conn, template_id="wf_ws-gate", title="X",
                role_map={"worker": "sshpeer", "checker": "peer"})
        assert len(_tasks(conn)) == before


def test_double_application_deduplicated(kanban_home, profiles):
    tid = "wf_incident-rca-repair-verify"
    with kbc.connect_closing() as conn:
        template = kwf.get_template(conn, board=None, template_id=tid)
        roles = _role_map_for(template)
        first = kwf.apply_workflow_template(
            conn, template_id=tid, title="Dedupe probe", role_map=roles,
            idempotency_key="wf:dedupe:1")
        count_after_first = len(_tasks(conn))
        second = kwf.apply_workflow_template(
            conn, template_id=tid, title="Dedupe probe", role_map=roles,
            idempotency_key="wf:dedupe:1")

        assert second.deduplicated is True
        assert second.root_task_id == first.root_task_id
        assert second.step_task_ids == first.step_task_ids
        assert len(_tasks(conn)) == count_after_first
        # Root keeps the caller's key; later steps carry traceable derived keys.
        root_row = conn.execute(
            "SELECT idempotency_key FROM tasks WHERE id = ?",
            (first.root_task_id,)).fetchone()
        assert root_row["idempotency_key"] == "wf:dedupe:1"
        for step, task_id in zip(template.steps, first.step_task_ids):
            row = conn.execute(
                "SELECT idempotency_key FROM tasks WHERE id = ?",
                (task_id,)).fetchone()
            expected = ("wf:dedupe:1" if step is template.steps[0]
                        else f"wf:dedupe:1::wf:{tid}:{step.step_key}")
            assert row["idempotency_key"] == expected
        # Exactly one audit event — the retry appends nothing.
        assert len(_events(conn, first.root_task_id, "workflow_applied")) == 1


def test_apply_key_bound_to_foreign_task_fails_closed(kanban_home, profiles):
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="plain card with the key",
                       assignee="peer", idempotency_key="wf:collide:1")
        before = len(_tasks(conn))
        with pytest.raises(ValueError, match="already bound"):
            kwf.apply_workflow_template(
                conn, template_id="wf_recon-implement-review", title="X",
                role_map={"recon": "peer", "implementer": "peer", "reviewer": "peer"},
                idempotency_key="wf:collide:1")
        assert len(_tasks(conn)) == before


def test_apply_extra_parents_gate_step_one(kanban_home, profiles):
    with kbc.connect_closing() as conn:
        gate = kb.create_task(conn, title="gate card", assignee="peer")
        template = kwf.get_template(conn, board=None,
                                    template_id="wf_recon-implement-review")
        applied = kwf.apply_workflow_template(
            conn, template_id="wf_recon-implement-review", title="Gated",
            role_map=_role_map_for(template), extra_parents=(gate,))
        root = kb.get_task(conn, applied.root_task_id)
        assert root.status == "todo"
        assert gate in _parents_of(conn, root.id)
        # Only step 1 carries the extra parent — the chain itself stays linear.
        first_child = _children(conn, root.id)
        assert len(first_child) == 1


def _parents_of(conn, tid):
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?", (tid,)).fetchall()
    return [r["parent_id"] for r in rows]


# ---------------------------------------------------------------- template CRUD


def test_template_validation_rejects_cycles_and_bad_payloads(kanban_home):
    dup = [
        {"step_key": "a", "title": "A", "assignee": "r1"},
        {"step_key": "b", "title": "B", "assignee": "r2"},
        {"step_key": "a", "title": "A again", "assignee": "r1"},
    ]
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="more than once"):
            kwf.create_template(conn, board=None, name="cycle", steps=dup)
        with pytest.raises(ValueError, match="steps must be a non-empty"):
            kwf.create_template(conn, board=None, name="empty", steps=[])
        with pytest.raises(ValueError, match="title is required"):
            kwf.create_template(conn, board=None, name="no title",
                                steps=[{"step_key": "a", "assignee": "r1"}])
        with pytest.raises(ValueError, match="approval_type"):
            kwf.create_template(conn, board=None, name="bad approval",
                                steps=[{"step_key": "a", "title": "A",
                                        "assignee": "r1", "approval_type": "nope"}])
        with pytest.raises(ValueError, match="unknown field"):
            kwf.create_template(conn, board=None, name="unknown field",
                                steps=[{"step_key": "a", "title": "A",
                                        "assignee": "r1", "parallel": True}])
        with pytest.raises(ValueError, match="must match"):
            kwf.create_template(conn, board=None, name="bad id",
                                steps=[{"step_key": "a", "title": "A", "assignee": "r1"}],
                                template_id="not-wf")
        assert kwf.list_templates(conn, board=None)  # reference set intact


def test_template_crud_roundtrip(kanban_home):
    steps = [
        {"step_key": "draft", "title": "Draft it", "assignee": "writer",
         "skills": ["available"], "priority": 5},
        {"step_key": "sign", "title": "Sign off", "assignee": "boss",
         "review": True, "approval_type": "action"},
    ]
    with kbc.connect_closing() as conn:
        created = kwf.create_template(conn, board=None, name="Doc flow",
                                      steps=steps, template_id="wf_doc-flow",
                                      created_by="alice")
        assert created.steps[0].skills == ("available",)
        assert created.steps[1].approval_type == "action"

        fetched = kwf.get_template(conn, board=None, template_id="wf_doc-flow")
        assert [s.step_key for s in fetched.steps] == ["draft", "sign"]

        with pytest.raises(ValueError, match="already exists"):
            kwf.create_template(conn, board=None, name="again",
                                steps=steps, template_id="wf_doc-flow")


# ---------------------------------------------------------------- old cards unchanged


def test_plain_create_unchanged_no_workflow_fields(kanban_home, profiles):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain", assignee="peer", body="b")
        task = kb.get_task(conn, tid)
        assert task.workflow_template_id is None
        assert task.current_step_key is None
        created = _events(conn, tid, "created")
        assert len(created) == 1
        assert "workflow_template_id" not in created[0].payload
        assert "current_step_key" not in created[0].payload
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE kind = 'workflow_applied'"
        ).fetchall()
        # A later workflow application never touches the older card.
        template = kwf.get_template(conn, board=None,
                                    template_id="wf_recon-implement-review")
        kwf.apply_workflow_template(
            conn, template_id="wf_recon-implement-review", title="later",
            role_map=_role_map_for(template))
        fresh = kb.get_task(conn, tid)
        assert fresh.workflow_template_id is None
        assert fresh.status == task.status
        assert not _events(conn, tid, "workflow_applied")


# ---------------------------------------------------------------- CLI + tool


def _cli_run(capsys, argv):
    """Parse like the real CLI and dispatch via kanban._HANDLERS; returns
    ``(rc, stdout, stderr)`` so error paths are assertable too."""
    import argparse

    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_parser import build_parser

    wrap = argparse.ArgumentParser(prog="kanban-wrap")
    parser = build_parser(wrap.add_subparsers(dest="_top"))
    args = parser.parse_args(argv)
    handler = kcli._HANDLERS[args.kanban_action]
    rc = handler(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_cli_workflow_template_roundtrip(kanban_home, capsys):
    rc, out, err = _cli_run(capsys, ["workflow-template", "list", "--json"])
    assert rc == 0, err
    listed = {t["id"] for t in json.loads(out)}
    assert set(REFERENCE) <= listed

    steps = [{"step_key": "one", "title": "One", "assignee": "r1"},
             {"step_key": "two", "title": "Two", "assignee": "r2", "review": True}]
    rc, out, err = _cli_run(capsys, ["workflow-template", "create", "Two step",
                                     "--steps", json.dumps(steps), "--id", "wf_two",
                                     "--json"])
    assert rc == 0, err
    assert json.loads(out)["id"] == "wf_two"

    rc, out, err = _cli_run(capsys, ["workflow-template", "show", "wf_two"])
    assert rc == 0 and "review" in out

    # Bad steps JSON is a clean CLI error, not a traceback.
    rc, out, err = _cli_run(capsys, ["workflow-template", "create", "Bad",
                                     "--steps", "{not json}"])
    assert rc == 2 and "--steps" in err

    # Argparse alias dispatch: `wf` reaches the same handler (root rule).
    from hermes_cli import kanban as kcli

    assert kcli._HANDLERS["wf"] is kcli._HANDLERS["workflow-template"]
    rc, out, err = _cli_run(capsys, ["wf", "list", "--json"])
    assert rc == 0 and "wf_two" in out


def test_cli_create_workflow_chain(kanban_home, capsys, profiles):
    rc, out, err = _cli_run(capsys, [
        "create", "Ship 2.0", "--workflow-template", "wf_recon-implement-review",
        "--role", "recon=peer", "--role", "implementer=peer2",
        "--role", "reviewer=peer", "--json"])
    assert rc == 0, err
    result = json.loads(out)
    assert result["template_id"] == "wf_recon-implement-review"
    assert [s["step_key"] for s in result["steps"]] == ["recon", "implement", "review"]

    with kbc.connect_closing() as conn:
        root = kb.get_task(conn, result["root_task_id"])
        assert root.status == "ready"
        assert root.assignee == "peer"

    # Conflicting per-task flags are refused before anything is created.
    rc, out, err = _cli_run(capsys, [
        "create", "Nope", "--workflow-template", "wf_recon-implement-review",
        "--role", "recon=peer", "--role", "implementer=peer",
        "--role", "reviewer=peer", "--assignee", "peer"])
    assert rc == 2 and "conflict" in (out + err)

    # Bad --role format is a clean error.
    rc, out, err = _cli_run(capsys, [
        "create", "Nope", "--workflow-template", "wf_recon-implement-review",
        "--role", "peer"])
    assert rc == 2 and "--role" in (out + err)


def test_tool_create_applies_workflow_chain(kanban_home, tmp_path, profiles):
    from tools import kanban_tools as kt

    result = json.loads(kt._handle_create({
        "title": "Tool instance",
        "workflow_template_id": "wf_recon-implement-review",
        "roles": {"recon": "peer", "implementer": "peer2", "reviewer": "peer"},
        "idempotency_key": "tool:wf:1",
    }))
    assert result["ok"] is True, result
    assert [s["step_key"] for s in result["steps"]] == [
        "recon", "implement", "review"]
    assert result["steps"][0]["status"] == "ready"

    # Retry with the same key reads the same chain back (lost response healed).
    retry = json.loads(kt._handle_create({
        "title": "Tool instance", "workflow_template_id": "wf_recon-implement-review",
        "roles": {"recon": "peer", "implementer": "peer2", "reviewer": "peer"},
        "idempotency_key": "tool:wf:1",
    }))
    assert retry["ok"] is True and retry["deduplicated"] is True
    assert retry["root_task_id"] == result["root_task_id"]

    # Conflicting args are refused; nothing is created.
    before = None
    with kbc.connect_closing() as conn:
        before = len(_tasks(conn))
    rejected = json.loads(kt._handle_create({
        "title": "Bad", "workflow_template_id": "wf_recon-implement-review",
        "roles": {"recon": "peer", "implementer": "peer", "reviewer": "peer"},
        "assignee": "peer",
    }))
    assert "conflicts" in rejected["error"]
    with kbc.connect_closing() as conn:
        assert len(_tasks(conn)) == before

    # Missing role mapping is a structured tool error, not a traceback.
    missing = json.loads(kt._handle_create({
        "title": "Bad", "workflow_template_id": "wf_recon-implement-review",
        "roles": {"recon": "peer"},
    }))
    assert "no profile is mapped" in missing["error"]
