#!/usr/bin/env bash
# Disposable heavy-test harness for t_ef2c4535 (governance stage 6:
# deterministic kanban routines via hermes cron).
# Heavy-Test-Rail: venv + HOME + HERMES_HOME + caches under a /var/tmp TEST_ROOT,
# nothing under ~ or NFS. The venv was built by run 166 from this same workspace
# via pm.build_env (editable install: source bytes are picked up live, verified
# by importing cron.kanban_routine from the workspace path). Build once, reuse
# across iterations, rm -rf after green (cleanup proof in receipt).
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
TEST_ROOT=/var/tmp/hermes-governance-routines.uX3VP9
export HOME="$TEST_ROOT/home"
export HERMES_HOME="$TEST_ROOT/hermes-home"
export UV_CACHE_DIR="$TEST_ROOT/cache/uv"
export PIP_CACHE_DIR="$TEST_ROOT/cache/pip"
export XDG_CACHE_HOME="$TEST_ROOT/cache/xdg"
export HERMES_PYTHON="$TEST_ROOT/venv/bin/python"
cd "$WS"

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
"$HERMES_PYTHON" -c "import cron.kanban_routine; print('VENV_IMPORT_OK')" || exit 5

run_suite () {
  local label="$1"; shift
  echo "== SUITE $label =="
  local rc=0
  scripts/run_tests.sh "$@" -q || rc=$?
  echo "SUITE_RC[$label]=$rc"
}

# --- new stage-6 tests ---
run_suite NEW tests/cron/test_kanban_routine.py

# --- cron regression: whole directory (scheduler/jobs/occurrences/catch-up/DST) ---
run_suite CRON_DIR tests/cron/

# --- cron CLI surfaces ---
run_suite CRON_CLI \
  tests/hermes_cli/test_cron.py \
  tests/hermes_cli/test_cron_parser_builder.py \
  tests/hermes_cli/test_cron_status_next_run.py \
  tests/hermes_cli/test_cron_dispatch_visibility.py \
  tests/hermes_cli/test_cron_delivery_targets_scope.py \
  tests/hermes_cli/test_cron_dashboard_off_loop.py \
  tests/hermes_cli/test_cron_fire_dashboard.py \
  tests/hermes_cli/test_cron_satellite_diagnostics.py \
  tests/hermes_cli/test_cron_status_profile_isolation.py

# --- cronjob tool surfaces ---
run_suite CRONJOB_TOOLS \
  tests/tools/test_cronjob_tools.py \
  tests/tools/test_cronjob_job_args.py \
  tests/tools/test_cronjob_run_immediate.py \
  tests/tools/test_cronjob_run_background.py \
  tests/tools/test_cronjob_run_delivery_notice.py \
  tests/tools/test_cron_approval_mode.py \
  tests/tools/test_cron_not_interactive.py \
  tests/tools/test_cron_prompt_injection.py

# --- kanban regression matrix (same lanes as the watchdog card) ---
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

# --- git hygiene proof ---
echo "== git diff --check =="
DIFFCHECK_RC=0
git diff --check || DIFFCHECK_RC=$?
echo "DIFFCHECK_RC=$DIFFCHECK_RC"

echo "END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "DONE"