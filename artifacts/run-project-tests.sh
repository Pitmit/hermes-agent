#!/usr/bin/env bash
# Disposable heavy-test harness for t_644132f9 (governance stage 3 projects).
# Heavy-Test-Rail: fresh venv + HOME + caches under a /var/tmp mktemp dir,
# nothing under ~ or NFS; trap-cleanup with proof; >= 5 GiB free proven first.
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
TEST_ROOT="$(mktemp -d /var/tmp/hermes-governance-proj.XXXXXX)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT INT TERM
echo "TEST_ROOT=$TEST_ROOT"

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

# --- new project-governance tests ---
echo "== new tests: test_kanban_projects.py =="
NEW_RC=0
scripts/run_tests.sh tests/hermes_cli/test_kanban_projects.py -q --tb=short || NEW_RC=$?
echo "NEW_RC=$NEW_RC"

# --- existing suites (project link / board project / cost budget / approvals /
#     review / request_changes / tenant / boards / block / sticky / unblock /
#     claim / pr-acceptance / tools / config / transfer) ---
SUITES="tests/hermes_cli/test_kanban_project_link.py \
tests/hermes_cli/test_kanban_board_project.py \
tests/hermes_cli/test_kanban_cost_budget.py \
tests/hermes_cli/test_kanban_approvals.py \
tests/hermes_cli/test_kanban_transfer.py \
tests/hermes_cli/test_kanban_cli.py \
tests/hermes_cli/test_kanban_db.py \
tests/hermes_cli/test_kanban_db_init.py \
tests/hermes_cli/test_kanban_core_functionality.py \
tests/hermes_cli/test_kanban_boards.py \
tests/hermes_cli/test_kanban_block_kinds.py \
tests/hermes_cli/test_kanban_blocked_sticky.py \
tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py \
tests/hermes_cli/test_kanban_pr_acceptance.py \
tests/hermes_cli/test_kanban_dispatch_claim_allowlist.py \
tests/hermes_cli/test_kanban_dispatch_lock.py \
tests/hermes_cli/test_kanban_dispatch_tick_hook.py \
tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py \
tests/hermes_cli/test_kanban_default_assignee.py \
tests/hermes_cli/test_kanban_review_lifecycle.py \
tests/hermes_cli/test_kanban_review_lifecycle_complete.py \
tests/hermes_cli/test_kanban_review_surfaces.py \
tests/hermes_cli/test_kanban_complete_live_claim_guard.py \
tests/hermes_cli/test_kanban_graph_identity.py \
tests/hermes_cli/test_kanban_specify.py \
tests/hermes_cli/test_config.py \
tests/tools/test_kanban_tools.py \
tests/tools/test_kanban_unknown_arguments.py \
tests/tools/test_kanban_toolset_opt_in.py"
echo "== existing suites =="
SUITES_RC=0
# shellcheck disable=SC2086
scripts/run_tests.sh $SUITES -q || SUITES_RC=$?
echo "SUITES_RC=$SUITES_RC"

# --- git hygiene proofs ---
echo "== git diff --check =="
DIFFCHECK_RC=0
git diff --check || DIFFCHECK_RC=$?
echo "DIFFCHECK_RC=$DIFFCHECK_RC"

echo "== projects_db.py untouched (spec section 5 criterion 3) =="
PROJECTSDB_RC=0
git diff --quiet -- hermes_cli/projects_db.py || PROJECTSDB_RC=$?
echo "PROJECTSDB_RC=$PROJECTSDB_RC (0 = clean)"

FREE_AFTER_KB="$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "FREE_AFTER_KB(before cleanup)=$FREE_AFTER_KB"
echo "OVERALL=$(( (NEW_RC == 0 && SUITES_RC == 0 && DIFFCHECK_RC == 0 && PROJECTSDB_RC == 0) ? 0 : 1 ))"
echo "DONE NEW_RC=$NEW_RC SUITES_RC=$SUITES_RC DIFFCHECK_RC=$DIFFCHECK_RC PROJECTSDB_RC=$PROJECTSDB_RC"
