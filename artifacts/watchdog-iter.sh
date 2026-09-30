#!/usr/bin/env bash
# Iteration harness for t_0e61b81b: build the disposable toolchain ONCE and
# keep it (NO trap) so failed iterations can rerun tests without a rebuild.
# The caller deletes WORK_ROOT when done (see cleanup line printed at end).
set -uo pipefail

WS=/home/hermes/.hermes/kanban/workspaces/hermes-kanban-governance
WORK_ROOT="${1:-/var/tmp/hermes-watchdog-iter}"
shift || true
mkdir -p "$WORK_ROOT"
echo "WORK_ROOT=$WORK_ROOT"
echo "START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

AVAIL_KB="$(df --output=avail -k /var/tmp | tail -1 | tr -d ' ')"
echo "FREE_BEFORE_KB=$AVAIL_KB"
if [ "${AVAIL_KB:-0}" -lt 5242880 ]; then
  echo "NO-GO: less than 5 GiB free on /var/tmp"; exit 3
fi

mkdir -p "$WORK_ROOT/home" "$WORK_ROOT/cache"
export HOME="$WORK_ROOT/home"
export HERMES_HOME="$WORK_ROOT/hermes-home"
export UV_CACHE_DIR="$WORK_ROOT/cache/uv"
export PIP_CACHE_DIR="$WORK_ROOT/cache/pip"
export XDG_CACHE_HOME="$WORK_ROOT/cache/xdg"
cd "$WS"

if [ ! -x "$WORK_ROOT/venv/bin/python" ]; then
  echo "== pm.build_env =="
  BUILD_RC=0
  python3 -m pm.build_env --source "$WS" --out "$WORK_ROOT/venv" --group dev --group test || BUILD_RC=$?
  echo "BUILD_RC=$BUILD_RC"
  [ "$BUILD_RC" -eq 0 ] || exit 4
else
  echo "== reusing $WORK_ROOT/venv =="
fi
export HERMES_PYTHON="$WORK_ROOT/venv/bin/python"

echo "== run: $* =="
RUN_RC=0
scripts/run_tests.sh "$@" -q --tb=short || RUN_RC=$?
echo "RUN_RC=$RUN_RC"
echo "END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "DONE RUN_RC=$RUN_RC (cleanup later: rm -rf $WORK_ROOT)"
