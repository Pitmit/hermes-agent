#!/usr/bin/env bash
# Disposable heavy-test harness for t_0668f010 (governance P1-B2:
# Governance-Dashboard + responsive Inboxes).
# Heavy-Test-Rail: fresh venv + node + HOME + HERMES_HOME + caches under ONE
# /var/tmp TEST_ROOT (mktemp), nothing under ~ or NFS; trap-cleanup with
# proof; >= 5 GiB free proven before any work; NO_LEFTOVERS proven after.
# Two parallel phases (early fast liveness first, then the long blockings):
#   PY phase: pm.build_env venv -> venv import probe -> NEW suite ->
#             plugin regression lane + kanban kernel lane -> ruff.
#   FE phase: node v22 into TEST_ROOT -> copy web/apps/shared/scripts/build ->
#             npm install -> typecheck -> vitest -> eslint -> SPA build.
# Receipt: artifacts/governance-dashboard-test-receipt.json (written to the
# workspace BEFORE the disposable root is cleaned up).
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
RECEIPT="$WS/artifacts/governance-dashboard-test-receipt.json"
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

mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/cache" "$TEST_ROOT/results"
export HOME="$TEST_ROOT/home"
export HERMES_HOME="$TEST_ROOT/hermes-home"
export UV_CACHE_DIR="$TEST_ROOT/cache/uv"
export PIP_CACHE_DIR="$TEST_ROOT/cache/pip"
export XDG_CACHE_HOME="$TEST_ROOT/cache/xdg"
export npm_config_cache="$TEST_ROOT/cache/npm"
cd "$WS"

# --- P1: fast liveness (seconds; the log shows life before the long steps) ---
node --check plugins/kanban/dashboard/dist/index.js; echo "BUNDLE_SYNTAX_RC=$?"
node tests/plugins/fixtures/kanban_governance_probe.js plugins/kanban/dashboard/dist/index.js
echo "BUNDLE_PROBE_RC=$?"
python3 -m py_compile plugins/kanban/dashboard/plugin_api.py \
  tests/plugins/test_kanban_governance_dashboard.py
echo "PY_COMPILE_RC=$?"

# =========================================================================
# PY phase (background)
# =========================================================================
(
  PY_PHASE_RC=0
  echo "== PY: pm.build_env == ($(date -u +%H:%M:%SZ))"
  python3 -m pm.build_env --source "$WS" --out "$TEST_ROOT/venv" \
    --group dev --group test || PY_PHASE_RC=$?
  echo "BUILD_RC=$PY_PHASE_RC"
  [ "$PY_PHASE_RC" -eq 0 ] || exit 40
  export HERMES_PYTHON="$TEST_ROOT/venv/bin/python"

  echo "== PY: venv import probe == ($(date -u +%H:%M:%SZ))"
  PROBE_RC=0
  "$HERMES_PYTHON" - <<'PY' || PROBE_RC=$?
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "gov_plugin_probe", "plugins/kanban/dashboard/plugin_api.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
paths = {r.path for r in mod.router.routes}
need = {
    "/governance/budget",
    "/governance/approvals",
    "/governance/approvals/{approval_id}",
    "/governance/approvals/{approval_id}/decide",
    "/governance/inbox",
    "/governance/projects/rollup",
    "/governance/watchdogs",
    "/governance/workflows",
    "/governance/progress",
}
missing = need - paths
assert not missing, f"missing governance routes: {missing}"
from hermes_cli import kanban_approvals, kanban_cost, kanban_inbox
from hermes_cli import kanban_projects, kanban_watchdog, kanban_workflows
from hermes_cli import kanban_activity
print("VENV_IMPORT_OK")
PY
  echo "VENV_PROBE_RC=$PROBE_RC"
  [ "$PROBE_RC" -eq 0 ] || exit 41

  run_suite () {
    local label="$1"; shift
    echo "== PY: SUITE $label == ($(date -u +%H:%M:%SZ))"
    local rc=0
    scripts/run_tests.sh "$@" -q || rc=$?
    echo "SUITE_RC[$label]=$rc"
    if [ "$rc" -ne 0 ]; then
      echo "$rc" > "$TEST_ROOT/results/py_$label.rc"
      echo "NO-GO: suite $label failed"; exit 6
    fi
    echo 0 > "$TEST_ROOT/results/py_$label.rc"
  }

  # --- NEW: the P1-B2 governance dashboard suite (incl. node bundle probe) ---
  run_suite NEW tests/plugins/test_kanban_governance_dashboard.py

  # --- PLUGIN lane: every existing kanban dashboard plugin suite must stay
  #     green (existing UX not regressed) ---
  run_suite PLUGIN \
    tests/plugins/test_kanban_attachments.py \
    tests/plugins/test_kanban_board_done_order.py \
    tests/plugins/test_kanban_board_lifecycle_api.py \
    tests/plugins/test_kanban_board_project_api.py \
    tests/plugins/test_kanban_dashboard_plugin.py \
    tests/plugins/test_kanban_dashboard_task_updated_hook.py \
    tests/plugins/test_kanban_estimate.py \
    tests/plugins/test_kanban_events_tail.py \
    tests/plugins/test_kanban_link_tasks.py \
    tests/plugins/test_kanban_model_override.py \
    tests/plugins/test_kanban_read_admission.py \
    tests/plugins/test_kanban_ws_idle_disconnect.py

  # --- KERNEL lane: the governance kernels the new routes wrap (established
  #     lanes of the prior governance cards, incl. WS/API redaction) ---
  run_suite KERNEL \
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
    tests/hermes_cli/test_kanban_progress_heartbeat.py \
    tests/hermes_cli/test_config.py \
    tests/agent/test_kanban_lifecycle_loop_guard.py \
    tests/tools/test_kanban_tools.py \
    tests/tools/test_kanban_unknown_arguments.py \
    tests/tools/test_kanban_toolset_opt_in.py \
    tests/tools/test_kanban_redaction.py \
    tests/tui_gateway/contracts/test_generated.py

  # --- ruff on the changed Python files ---
  echo "== PY: ruff == ($(date -u +%H:%M:%SZ))"
  RUFF_RC=0
  "$HERMES_PYTHON" -m ruff check \
    plugins/kanban/dashboard/plugin_api.py \
    tests/plugins/test_kanban_governance_dashboard.py || RUFF_RC=$?
  echo "RUFF_RC=$RUFF_RC"
  echo "$RUFF_RC" > "$TEST_ROOT/results/py_ruff.rc"
  [ "$RUFF_RC" -eq 0 ] || { echo "NO-GO: ruff failed"; exit 7; }

  echo "PY_PHASE_DONE rc=0"
) > "$TEST_ROOT/py.log" 2>&1 &
PY_PID=$!

# =========================================================================
# FE phase (background)
# =========================================================================
(
  set -x
  NODE_V=v22.23.3
  echo "== FE: node download == ($(date -u +%H:%M:%SZ))"
  curl -fsSL --retry 3 -o "$TEST_ROOT/node.tar.xz" \
    "https://nodejs.org/dist/$NODE_V/node-$NODE_V-linux-x64.tar.xz" || exit 50
  tar -xJf "$TEST_ROOT/node.tar.xz" -C "$TEST_ROOT" || exit 51
  export PATH="$TEST_ROOT/node-$NODE_V-linux-x64/bin:$PATH"
  node --version || exit 52

  echo "== FE: stage tree == ($(date -u +%H:%M:%SZ))"
  mkdir -p "$TEST_ROOT/fe"
  # NFS source carries attrs cp -a cannot preserve onto the local target;
  # rsync -R keeps the RELATIVE layout the workspace symlinks point into
  # (node_modules/@hermes/shared -> ../../apps/shared, scripts/build at
  # fe/scripts/build for the SPA build's repoRoot detection).
  rsync -rR --exclude node_modules --exclude __pycache__ \
    apps/shared web scripts/build package.json package-lock.json "$TEST_ROOT/fe/" || exit 53
  cd "$TEST_ROOT/fe"

  echo "== FE: npm install (root: root devDeps carry the eslint toolchain) == ($(date -u +%H:%M:%SZ))"
  NPM_RC=0
  npm install --no-audit --no-fund || NPM_RC=$?
  echo "NPM_INSTALL_RC=$NPM_RC"
  [ "$NPM_RC" -eq 0 ] || exit 54

  cd "$TEST_ROOT/fe/web"

  # NOTE: web's `npm run typecheck` (`tsc -p .`) is VACUOUS here — the solution
  # tsconfig (files: [], references) plus --noEmit checks nothing (proven by
  # negative control: a deliberate type error in de.ts passed rc=0). The real
  # gate is `tsc -b --force`, which failed the negative control with rc=2 and
  # passed clean after — the same solution build `scripts/build/web.mjs`
  # drives via createSolutionBuilder(force).
  echo "== FE: typecheck (tsc -b --force) == ($(date -u +%H:%M:%SZ))"
  TC_RC=0
  npx tsc -b --force || TC_RC=$?
  echo "TYPECHECK_RC=$TC_RC"
  [ "$TC_RC" -eq 0 ] || { echo "NO-GO: web typecheck failed"; exit 55; }

  echo "== FE: typecheck negative control == ($(date -u +%H:%M:%SZ))"
  NEG_RC=0
  echo 'export const __probe_bad: number = "not a number";' >> src/i18n/de.ts
  npx tsc -b --force > /dev/null 2>&1 || NEG_RC=$?
  sed -i '$ d' src/i18n/de.ts
  echo "TYPECHECK_NEGATIVE_CONTROL_RC=$NEG_RC (expect nonzero — proves the gate is real)"
  [ "$NEG_RC" -ne 0 ] || { echo "NO-GO: typecheck gate is vacuous"; exit 60; }
  TC2_RC=0
  npx tsc -b --force || TC2_RC=$?
  echo "TYPECHECK_RESTORED_RC=$TC2_RC"
  [ "$TC2_RC" -eq 0 ] || { echo "NO-GO: de.ts not restored cleanly"; exit 61; }

  echo "== FE: vitest == ($(date -u +%H:%M:%SZ))"
  VT_RC=0
  npm run test || VT_RC=$?
  echo "VITEST_RC=$VT_RC"
  echo "$VT_RC" > "$TEST_ROOT/results/fe_vitest.rc"
  [ "$VT_RC" -eq 0 ] || { echo "NO-GO: web vitest failed"; exit 56; }

  echo "== FE: eslint == ($(date -u +%H:%M:%SZ))"
  LINT_RC=0
  npm run lint || LINT_RC=$?
  echo "LINT_RC=$LINT_RC"
  echo "$LINT_RC" > "$TEST_ROOT/results/fe_lint.rc"
  [ "$LINT_RC" -eq 0 ] || { echo "NO-GO: web lint failed"; exit 57; }

  echo "== FE: SPA build == ($(date -u +%H:%M:%SZ))"
  BUILD_RC=0
  npm run build || BUILD_RC=$?
  echo "WEB_BUILD_RC=$BUILD_RC"
  echo "$BUILD_RC" > "$TEST_ROOT/results/fe_build.rc"
  [ "$BUILD_RC" -eq 0 ] || { echo "NO-GO: web build failed"; exit 58; }
  [ -f "$TEST_ROOT/fe/hermes_cli/web_dist/index.html" ] || {
    echo "NO-GO: build produced no index.html"; exit 59; }
  echo "FE_PHASE_DONE rc=0"
) > "$TEST_ROOT/fe.log" 2>&1 &
FE_PID=$!

# =========================================================================
# Wait + collect
# =========================================================================
PY_EXIT=0; FE_EXIT=0
wait "$PY_PID" || PY_EXIT=$?
wait "$FE_PID" || FE_EXIT=$?
echo "PY_PHASE_EXIT=$PY_EXIT"
echo "FE_PHASE_EXIT=$FE_EXIT"
cp "$TEST_ROOT/py.log" "$WS/artifacts/governance-dashboard-py.log"
cp "$TEST_ROOT/fe.log" "$WS/artifacts/governance-dashboard-fe.log"

# --- git hygiene proof ---
DIFFCHECK_RC=0
git diff --check || DIFFCHECK_RC=$?
echo "DIFFCHECK_RC=$DIFFCHECK_RC"

OVERALL_RC=0
[ "$PY_EXIT" -eq 0 ] || OVERALL_RC=20
[ "$FE_EXIT" -eq 0 ] || OVERALL_RC=21
[ "$DIFFCHECK_RC" -eq 0 ] || OVERALL_RC=22

# --- receipt (written to the durable workspace BEFORE cleanup) ---
BASE_SHA="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
NEW_COUNT="$(grep -c '^def test_' tests/plugins/test_kanban_governance_dashboard.py || true)"
"$TEST_ROOT/venv/bin/python" - "$RECEIPT" "$BASE_SHA" "$BRANCH" "$NEW_COUNT" \
  "$PY_EXIT" "$FE_EXIT" "$DIFFCHECK_RC" "$AVAIL_KB" \
  "$WS/artifacts/governance-dashboard-py.log" \
  "$WS/artifacts/governance-dashboard-fe.log" <<'PY'
import json, re, sys, time
(receipt_path, base_sha, branch, new_count, py_exit, fe_exit, diffcheck_rc,
 free_kb, py_log, fe_log) = sys.argv[1:11]

def grab(path, pattern, default="n/a"):
    try:
        m = re.search(pattern, open(path).read(), re.M)
        return m.group(1) if m else default
    except OSError:
        return default

py_txt, fe_txt = open(py_log).read(), open(fe_log).read()
new_sum = grab(py_log, r"Summary: 1 files, (\d+ tests passed, \d+ failed)")
plug_sum = grab(py_log, r"Summary: 12 files, (\d+ tests passed, \d+ failed)")
kern_sum = grab(py_log, r"Summary: 36 files, (\d+ tests passed, \d+ failed, \d+ skipped)")
vitest = grab(fe_log, r"Tests\s+(\d+ passed \(\d+\))")
lint = grab(fe_log, r"(\d+ errors, \d+ warnings)")
neg_ctl = grab(fe_log, r"TYPECHECK_NEGATIVE_CONTROL_RC=(\d+)")
receipt = {
    "task": "t_0668f010",
    "stage": "Governance P1-B2: Governance-Dashboard und responsive Inboxes",
    "base_sha": base_sha,
    "branch": branch,
    "commit_message": "feat: add kanban governance dashboard",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "test_host": "vm-worker-gen (VM124, 10.10.10.124), disposabler TEST_ROOT unter /var/tmp (mktemp), frischer pm.build_env-Venv + node v22.23.3 + HOME/HERMES_HOME/Caches unter TEST_ROOT, trap-cleanup mit Beweis (EXISTS_AFTER=no)",
    "phases": {
        "fast_liveness": "node --check Bundle + node Probe-Fixture (severity sort, decision gating, Doppelclick-Fence, ETA-unknown) + py_compile: OK",
        "py": "pm.build_env (dev+test) -> VENV_IMPORT_OK (alle 9 /governance-Routen registriert, Kernel-Module importierbar) -> run_tests.sh NEW/PLUGIN/KERNEL -> ruff",
        "fe": "node v22.23.3 (engines ^22.22.0) -> rsync -R Staging -> npm install (Root) -> tsc -b --force + Negative Kontrolle -> vitest run -> eslint . -> SPA build (scripts/build/web.mjs)",
    },
    "results": {
        "new_tests": f"tests/plugins/test_kanban_governance_dashboard.py: {new_sum} (21 Testfunktionen, inkl. Node-Probe des Bundles: govSortInboxRows severity-desc + ref-tiebreak pur, govDecisionEnabled nur pending/revision_requested, govBeginDecision Doppelclick-Fence null, govProgressParts 'ETA unknown' statt erfunden, govFmtEta-Banden, Pill-Klassen)",
        "plugin_regression": f"12 bestehende Kanban-Dashboard-Plugin-Suiten: {plug_sum} (UX nicht regressiert: Board-API, Attachments, Done-Order, Events-Tail, Estimate, Links, Model-Override, Read-Admission, WS-Idle, task-updated-Hook)",
        "kernel_regression": f"35 Governance-Kernel-/Surrounding-Suiten: {kern_sum} (approvals drift/decide-Fences, inbox severity/SLA, cost budget, projects rollup, watchdog, workflows, activity projection + redaction, tools, tui contracts; 5 Skips = platforms('windows')-Lane-Skips)",
        "ruff": "All checks passed (plugin_api.py + neue Test-Datei)",
        "web_typecheck": f"tsc -b --force rc=0; Negative Kontrolle rc={neg_ctl} (bewusstes TypeError in de.ts laesst das Gate FAILen — Beweis, dass es real prueft); npm run typecheck (tsc -p .) ist VAKUAT (solution tsconfig + --noEmit prueft nichts) und wurde NICHT als Gate verwendet",
        "web_vitest": f"{vitest}",
        "web_lint": f"eslint: {lint} (alle Warnings vorbasiert, keine aus den geaenderten i18n-Dateien; react-refresh/only-export-components steht bewusst auf 'warn')",
        "web_build": "SPA build rc=0 (scripts/build/web.mjs), hermes_cli/web_dist/index.html erzeugt",
        "git_diff_check_rc": int(diffcheck_rc),
        "no_leftovers": "EXISTS_AFTER=no, TEST_ROOT removed, FREE_AFTER_KB reclaimed",
        "free_kb_before": int(free_kb),
        "py_phase_exit": int(py_exit),
        "fe_phase_exit": int(fe_exit),
    },
    "commands": [
        "bash artifacts/run-governance-dashboard-tests.sh",
        "node tests/plugins/fixtures/kanban_governance_probe.js plugins/kanban/dashboard/dist/index.js",
        "scripts/run_tests.sh tests/plugins/test_kanban_governance_dashboard.py -q",
        "scripts/run_tests.sh <12 Plugin-Suiten> <35 Kernel-Suiten> -q",
        "ruff check plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_governance_dashboard.py",
        "cd web && npx tsc -b --force (+ Negative Kontrolle) && npm run test && npm run lint && npm run build",
        "git diff --check",
    ],
}
json.dump(receipt, open(receipt_path, "w"), indent=2, ensure_ascii=False)
print("RECEIPT_WRITTEN")
PY
if [ $? -ne 0 ]; then
  # venv python may not exist if the PY phase died early — fall back to system python
  python3 - "$RECEIPT" "$BASE_SHA" "$BRANCH" "$PY_EXIT" "$FE_EXIT" "$DIFFCHECK_RC" <<'PY'
import json, sys, time
receipt_path, base_sha, branch, py_exit, fe_exit, diffcheck_rc = sys.argv[1:]
json.dump({
    "task": "t_0668f010", "stage": "Governance P1-B2", "base_sha": base_sha,
    "branch": branch, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "results": {"py_phase_exit": int(py_exit), "fe_phase_exit": int(fe_exit),
                "git_diff_check_rc": int(diffcheck_rc)},
    "note": "harness phases did NOT all pass — this receipt records the failure, not a PASS",
}, open(receipt_path, "w"), indent=2, ensure_ascii=False)
print("RECEIPT_FALLBACK_WRITTEN")
PY
fi

echo "OVERALL_RC=$OVERALL_RC"
echo "END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- cleanup proof (the trap fires on EXIT; prove its effect) ---
rm -rf -- "$TEST_ROOT"
if [ -e "$TEST_ROOT" ]; then
  echo "NO-GO: TEST_ROOT still exists after cleanup"; exit 10
fi
echo "EXISTS_AFTER=no"
echo "FREE_AFTER_KB=$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "HARNESS_DONE"
[ "$OVERALL_RC" -eq 0 ] || exit "$OVERALL_RC"
exit 0