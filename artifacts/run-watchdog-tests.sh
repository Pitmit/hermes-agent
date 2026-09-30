#!/usr/bin/env bash
# Disposable heavy-test harness for t_0e61b81b (governance stage 5:
# task-bound independent watchdog).
# Heavy-Test-Rail: fresh venv + HOME + caches under a /var/tmp mktemp dir,
# nothing under ~ or NFS; trap-cleanup with proof; >= 5 GiB free proven first.
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
TEST_ROOT="$(mktemp -d /var/tmp/hermes-governance-watchdog.XXXXXX)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT INT TERM
echo "TEST_ROOT=$TEST_ROOT"
echo "START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- free-space proof (>= 5 GiB) ---
AVAIL_KB="$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "FREE_BEFORE_KB=$AVAIL_KB"
if [ "${AVAIL_KB:-0}" -lt 5242880 ]; then
  echo "NO-GO: less than 5 GiB free on /var/tmp"; exit 3
fi

mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/cache"
export HOME="$TEST_ROOT/home"
export HERMES_HOME="$TEST_ROOT/hermes-home"
export UV_CACHE_DIR="$TEST_ROOT/cache/uv"
export PIP_CACHE_DIR="$TEST_ROOT/cache/pip"
export XDG_CACHE_HOME="$TEST_ROOT/cache/xdg"
cd "$WS"

# --- fresh disposable toolchain (local, never NFS) ---
echo "== pm.build_env =="
BUILD_RC=0
python3 -m pm.build_env --source "$WS" --out "$TEST_ROOT/venv" --group dev --group test || BUILD_RC=$?
echo "BUILD_RC=$BUILD_RC"
[ "$BUILD_RC" -eq 0 ] || exit 4

export HERMES_PYTHON="$TEST_ROOT/venv/bin/python"

# --- new stage-5 tests ---
echo "== new tests: test_kanban_watchdog.py =="
NEW_RC=0
scripts/run_tests.sh tests/hermes_cli/test_kanban_watchdog.py -q --tb=short || NEW_RC=$?
echo "NEW_RC=$NEW_RC"

# --- existing suites: review lifecycle, sticky gates, block kinds,
#     approvals, inbox, projects, cost/budget, db, CLI, tools, config,
#     lifecycle loop guard (previous card), redaction ---
SUITES="tests/hermes_cli/test_kanban_watchdog.py \
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
tests/tools/test_kanban_redaction.py"
echo "== existing suites (incl. new file for cross-suite isolation) =="
SUITES_RC=0
# shellcheck disable=SC2086
scripts/run_tests.sh $SUITES -q || SUITES_RC=$?
echo "SUITES_RC=$SUITES_RC"

# --- git hygiene proofs ---
echo "== git diff --check =="
DIFFCHECK_RC=0
git diff --check || DIFFCHECK_RC=$?
echo "DIFFCHECK_RC=$DIFFCHECK_RC"

FREE_AFTER_KB="$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "FREE_AFTER_KB(before cleanup)=$FREE_AFTER_KB"
echo "OVERALL=$(( (NEW_RC == 0 && SUITES_RC == 0 && DIFFCHECK_RC == 0) ? 0 : 1 ))"
echo "END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "DONE NEW_RC=$NEW_RC SUITES_RC=$SUITES_RC DIFFCHECK_RC=$DIFFCHECK_RC"
