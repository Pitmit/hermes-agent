#!/usr/bin/env bash
# Disposable heavy-test harness for t_023152dd (governance stage 6, P1-A3:
# user-definable kanban workflow templates + activation of the dormant
# workflow_template_id/current_step_key plumbing).
# Heavy-Test-Rail: venv + HOME + HERMES_HOME + caches under one /var/tmp
# TEST_ROOT, nothing under ~ or NFS. The venv was built fresh for this task via
# pm.build_env from this same workspace (editable install: source bytes are
# picked up live, VENV_IMPORT_OK proven below before the matrix). rm -rf after
# green with proof (EXISTS_AFTER / df recovery below).
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
TEST_ROOT="${WF_TEST_ROOT:-/var/tmp/hermes-test.9xxMxD}"
export HOME="$TEST_ROOT/home"
export HERMES_HOME="$TEST_ROOT/hermes-home"
export UV_CACHE_DIR="$TEST_ROOT/cache/uv"
export PIP_CACHE_DIR="$TEST_ROOT/cache/pip"
export XDG_CACHE_HOME="$TEST_ROOT/cache/xdg"
export HERMES_PYTHON="$TEST_ROOT/venv/bin/python"
cd "$WS"

# Rail: trap the cleanup BEFORE any work; prove the root exists right now.
trap 'rm -rf -- "$TEST_ROOT"; echo "TRAP_CLEANUP_RAN=$(date -u +%Y-%m-%dT%H:%M:%SZ)"' EXIT INT TERM

echo "TEST_ROOT=$TEST_ROOT"
echo "START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- free-space proof (>= 5 GiB) ---
AVAIL_KB="$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "FREE_BEFORE_KB=$AVAIL_KB"
if [ "${AVAIL_KB:-0}" -lt 5242880 ]; then
  echo "NO-GO: less than 5 GiB free on /var/tmp"; exit 3
fi
if [ ! -x "$HERMES_PYTHON" ]; then
  echo "NO-GO: disposable venv missing at $HERMES_PYTHON"; exit 4
fi
"$HERMES_PYTHON" -c "import hermes_cli.kanban_workflows as w; assert len(w._REFERENCE_TEMPLATES) == 5; print('VENV_IMPORT_OK editable:', w.__file__)" || exit 5

run_suite () {
  local label="$1"; shift
  echo "== SUITE $label =="
  local rc=0
  scripts/run_tests.sh "$@" -q || rc=$?
  echo "SUITE_RC[$label]=$rc"
  if [ "$rc" -ne 0 ]; then
    echo "NO-GO: suite $label failed"; exit 6
  fi
}

# --- new stage-6 P1-A3 tests ---
run_suite NEW tests/hermes_cli/test_kanban_workflows.py

# --- kanban regression matrix (same lanes as the watchdog/routine cards) ---
run_suite KANBAN \
  tests/hermes_cli/test_kanban_watchdog.py \
  tests/hermes_cli/test_kanban_approvals.py \
  tests/hermes_cli/test_kanban_inbox.py \
  tests/hermes_cli/test_kanban_blocked_sticky.py \
  tests/hermes_cli/test_kanban_block_kinds.py \
  tests/hermes_cli/test_kanban_review_lifecycle.py \
  tests/hermes_cli/test_kanban_review_surfaces.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_db_init.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_boards.py \
  tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py \
  tests/hermes_cli/test_kanban_pr_acceptance.py \
  tests/hermes_cli/test_kanban_project_link.py \
  tests/hermes_cli/test_kanban_projects.py \
  tests/hermes_cli/test_kanban_cost_budget.py \
  tests/hermes_cli/test_config.py \
  tests/agent/test_kanban_lifecycle_loop_guard.py \
  tests/tools/test_kanban_tools.py \
  tests/tools/test_kanban_unknown_arguments.py \
  tests/tools/test_kanban_toolset_opt_in.py \
  tests/tools/test_kanban_redaction.py

# --- spec acceptance guard: no second scheduler/tick loop in the delta. The
# spec's literal test is "while True … sleep" (a poll/tick engine); the new
# module's `_recover_chain` walk is a bounded graph traversal (breaks when the
# chain ends, no sleep, no polling), not a scheduler.
echo "== acceptance: no new tick/scheduler loop in the delta =="
LOOP_GREP_RC=0
git diff -U0 | grep -nE '^\+.*while +True' && LOOP_GREP_RC=1 || true
# A scheduler/tick loop needs a sleep/poll primitive; the new module has none
# (its while-loop is the bounded `_recover_chain` graph walk). Sleep-free ⇒
# no second scheduler engine, whatever the loop shape.
grep -nE 'sleep\(|\.poll\(' hermes_cli/kanban_workflows.py && LOOP_GREP_RC=1 || true
echo "LOOP_GREP_RC=$LOOP_GREP_RC (0 = no added scheduler loop; new module is sleep-free)"
if [ "$LOOP_GREP_RC" -ne 0 ]; then
  echo "NO-GO: new scheduler-style loop in the delta"; exit 7
fi

# --- git hygiene proof ---
echo "== git diff --check =="
DIFFCHECK_RC=0
git diff --check || DIFFCHECK_RC=$?
echo "DIFFCHECK_RC=$DIFFCHECK_RC"
if [ "$DIFFCHECK_RC" -ne 0 ]; then
  echo "NO-GO: git diff --check not clean"; exit 8
fi

echo "END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- cleanup proof (the trap fires on EXIT; prove its effect) ---
rm -rf -- "$TEST_ROOT"
if [ -e "$TEST_ROOT" ]; then
  echo "NO-GO: TEST_ROOT still exists after cleanup"; exit 9
fi
echo "EXISTS_AFTER=no"
echo "FREE_AFTER_KB=$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "HARNESS_DONE"
