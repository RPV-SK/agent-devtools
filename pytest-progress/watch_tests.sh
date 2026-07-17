#!/bin/bash
# Watch a PV_TEST_PROGRESS=1 pytest run: prints the currently-running test and
# its elapsed seconds once per second. Ctrl-C stops watching; the run is
# unaffected. Usage:
#   PV_TEST_PROGRESS=1 python -m pytest -q &     # or in another shell
#   ./watch_tests.sh [path-to-.pytest-progress]
PROGRESS_FILE="${1:-.pytest-progress}"
while true; do
  if [ -f "$PROGRESS_FILE" ]; then
    python3 - "$PROGRESS_FILE" <<'EOF'
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    print(f"\r{d['current']}  ({time.time() - d['started_at']:.0f}s)", end="", flush=True)
except Exception:
    pass
EOF
  else
    printf '\r(no run in progress — start pytest with PV_TEST_PROGRESS=1)   '
  fi
  sleep 1
done
