"""Live test progress for pytest — paste into your conftest.py.

Opt-in via PV_TEST_PROGRESS=1 (rename the env var to taste): streams the
currently-running test nodeid + start time to `.pytest-progress` at the repo
root (JSON, overwritten per test, removed at session end). Lets an
orchestrator — human or agent — distinguish a hung suite from a slow one
before killing it. Zero dependencies, zero cost when the env var is off.

Add `.pytest-progress` to .gitignore.
"""

import os

if os.environ.get("PV_TEST_PROGRESS") == "1":
    import json
    import time
    from pathlib import Path

    # Adjust to your layout: this assumes conftest.py lives in tests/.
    _PROGRESS_FILE = Path(__file__).resolve().parent.parent / ".pytest-progress"

    def pytest_runtest_logstart(nodeid, location):
        _PROGRESS_FILE.write_text(json.dumps({
            "current": nodeid,
            "started_at": time.time(),
            "pid": os.getpid(),
        }))

    def pytest_sessionfinish(session, exitstatus):
        try:
            _PROGRESS_FILE.unlink()
        except OSError:
            pass
