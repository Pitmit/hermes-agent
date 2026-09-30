#!/usr/bin/env bash
# Disposable heavy-test harness for t_a9ba6847 (governance P1-B1:
# structured progress + ETA heartbeats).
# Heavy-Test-Rail: fresh venv + HOME + HERMES_HOME + caches under one
# /var/tmp TEST_ROOT (mktemp), nothing under ~ or NFS; trap-cleanup with
# proof; >= 5 GiB free proven before any work; NO_LEFTOVERS proven after.
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
TEST_ROOT="$(mktemp -d /var/tmp/hermes-test.XXXXXX)"
trap 'rm -rf -- "$TEST_ROOT"; echo "TRAP_CLEANUP_RAN=$(date -u +%Y-%m-%dT%H:%M:%SZ)"' EXIT INT TERM
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

# --- editable-install proof: the new symbols import and validate live ---
echo "== venv import probe =="
PROBE_RC=0
"$HERMES_PYTHON" - <<'PY' || PROBE_RC=$?
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_activity
from tui_gateway.contracts.kanban_activity import KanbanActivityRun
f = kbd.normalize_progress_heartbeat(
    {"phase": " encode ", "completed": 4, "total": 8, "unit": "files",
     "rate": 0.5, "eta_seconds": 300, "error_count": 1})
assert f["phase"] == "encode" and f["progress_pct"] == 50, f
try:
    kbd.normalize_progress_heartbeat({"completed": float("nan")})
    raise SystemExit("NaN accepted")
except ValueError:
    pass
r = KanbanActivityRun(run_id=1, phase="encode", completed=4, total=8,
                     progress_pct=50, eta_seconds=300)
assert r.progress_pct == 50
assert kanban_activity.ACTIVITY_PHASE_MAX_CHARS == 80
print("VENV_IMPORT_OK")
PY
echo "PROBE_RC=$PROBE_RC"
[ "$PROBE_RC" -eq 0 ] || exit 5

run_suite () {
  local label="$1"; shift
  echo "== SUITE $label == ($(date -u +%H:%M:%SZ))"
  local rc=0
  scripts/run_tests.sh "$@" -q || rc=$?
  echo "SUITE_RC[$label]=$rc"
  if [ "$rc" -ne 0 ]; then
    echo "NO-GO: suite $label failed"; exit 6
  fi
}

# --- new P1-B1 tests ---
run_suite NEW tests/hermes_cli/test_kanban_progress_heartbeat.py

# --- kanban regression matrix (established lanes of the prior governance
#     cards + the two surfaces this card touches: activity projection and the
#     generated gateway contracts) ---
run_suite KANBAN \
  tests/hermes_cli/test_kanban_activity_projection.py \
  tests/hermes_cli/test_kanban_approvals.py \
  tests/hermes_cli/test_kanban_inbox.py \
  tests/hermes_cli/test_kanban_blocked_sticky.py \
  tests/hermes_cli/test_kanban_block_kinds.py \
  tests/hermes_cli/test_kanban_review_lifecycle.py \
  tests/hermes_cli/test_kanban_review_lifecycle_complete.py \
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
  tests/hermes_cli/test_kanban_graph_identity.py \
  tests/hermes_cli/test_kanban_specify.py \
  tests/hermes_cli/test_kanban_dispatch_claim_allowlist.py \
  tests/hermes_cli/test_kanban_dispatch_lock.py \
  tests/hermes_cli/test_kanban_dispatch_tick_hook.py \
  tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py \
  tests/hermes_cli/test_kanban_default_assignee.py \
  tests/hermes_cli/test_kanban_complete_live_claim_guard.py \
  tests/hermes_cli/test_kanban_workflows.py \
  tests/hermes_cli/test_kanban_watchdog.py \
  tests/hermes_cli/test_config.py \
  tests/agent/test_kanban_lifecycle_loop_guard.py \
  tests/tools/test_kanban_tools.py \
  tests/tools/test_kanban_unknown_arguments.py \
  tests/tools/test_kanban_toolset_opt_in.py \
  tests/tools/test_kanban_redaction.py \
  tests/tui_gateway/contracts/test_generated.py

# --- ruff on the changed Python files ---
echo "== ruff =="
RUFF_RC=0
"$HERMES_PYTHON" -m ruff check \
  tools/kanban_tools.py tools/kanban_tools_schemas.py \
  hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py \
  hermes_cli/kanban_db_connect.py hermes_cli/kanban_activity.py \
  hermes_cli/kanban_output.py agent/prompt_builder.py \
  tui_gateway/contracts/kanban_activity.py \
  plugins/kanban/dashboard/plugin_api.py \
  tests/hermes_cli/test_kanban_progress_heartbeat.py \
  tests/tools/test_kanban_tools.py || RUFF_RC=$?
echo "RUFF_RC=$RUFF_RC"
[ "$RUFF_RC" -eq 0 ] || { echo "NO-GO: ruff failed"; exit 7; }

# --- acceptance guard: existing polling loop reused, no new scheduler/tick.
#     The projection rides get_activity_snapshot inside the WS/API poller the
#     dashboard already polls; the delta must not add a loop/poll primitive.
echo "== acceptance: no new polling/tick loop in the delta =="
LOOP_GREP_RC=0
DELTA="$(mktemp)"
git diff -U0 -- hermes_cli/kanban_activity.py tui_gateway/ ui-tui/ plugins/kanban/ > "$DELTA"
# Loop/timer primitives must not appear in added lines at all...
grep -nE '^[+].*(while|sleep|setInterval|setTimeout|tick)' "$DELTA" && LOOP_GREP_RC=1 || true
# ...and no new poller is DEFINED (the delta extends the existing snapshot
# the poller already fetches; "poller" appears only in prose comments).
grep -nE '^[+].*(def [a-zA-Z_]*poll|poll_loop|startPolling)' "$DELTA" && LOOP_GREP_RC=1 || true
rm -f "$DELTA"
echo "LOOP_GREP_RC=$LOOP_GREP_RC (0 = projection reuses the existing poller)"
[ "$LOOP_GREP_RC" -eq 0 ] || { echo "NO-GO: new loop/poll primitive in the delta"; exit 8; }

# --- git hygiene proof ---
echo "== git diff --check =="
DIFFCHECK_RC=0
git diff --check || DIFFCHECK_RC=$?
echo "DIFFCHECK_RC=$DIFFCHECK_RC"
[ "$DIFFCHECK_RC" -eq 0 ] || { echo "NO-GO: git diff --check not clean"; exit 9; }

echo "END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- cleanup proof (the trap fires on EXIT; prove its effect) ---
rm -rf -- "$TEST_ROOT"
if [ -e "$TEST_ROOT" ]; then
  echo "NO-GO: TEST_ROOT still exists after cleanup"; exit 10
fi
echo "EXISTS_AFTER=no"
echo "FREE_AFTER_KB=$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "HARNESS_DONE"
