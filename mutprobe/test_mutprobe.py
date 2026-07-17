"""Hermetic tests of scripts/mutprobe.py — the declared-mutation harness.

Every test runs against a throwaway mini-project built in tmp_path (its own
module + pytest file + manifest); the real project code is never touched.
mutprobe is driven as a subprocess, exactly as the CLI runs it, so exit codes,
stdout table, restore behavior and signal handling are all tested end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

MUTPROBE = Path(__file__).resolve().parent.parent / "scripts" / "mutprobe.py"

MOD_SRC = textwrap.dedent("""\
    def add(a, b):
        return a + b


    def helper():
        return "spare"
    """)

TEST_SRC = textwrap.dedent("""\
    from mod import add

    def test_add():
        assert add(2, 3) == 5
    """)


def make_project(tmp_path: Path, mod_src: str = MOD_SRC,
                 test_src: str = TEST_SRC) -> None:
    (tmp_path / "mod.py").write_text(mod_src)
    (tmp_path / "test_mod.py").write_text(test_src)


def write_manifest(tmp_path: Path, probes: list[dict]) -> Path:
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(probes))
    return mpath


def run_mutprobe(tmp_path: Path, *extra: str,
                 timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MUTPROBE),
         "--root", str(tmp_path),
         "--manifest", str(tmp_path / "manifest.json"),
         "--python", sys.executable,
         *extra],
        capture_output=True, text=True, timeout=timeout,
    )


PROBE_ADD = {
    "id": "flip-add",
    "file": "mod.py",
    "find": "return a + b",
    "replace": "return a - b",
    "tests": ["test_mod.py::test_add"],
    "note": "caught by test_add",
}


# --------------------------------------------------------------- happy path


def test_red_probe_exits_zero_and_restores_bytes(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path, [PROBE_ADD])
    before = (tmp_path / "mod.py").read_bytes()
    res = run_mutprobe(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "RED" in res.stdout
    assert "all probes RED" in res.stdout
    assert (tmp_path / "mod.py").read_bytes() == before


def test_green_survivor_exits_two(tmp_path):
    make_project(tmp_path)
    # mutates helper(), which test_add never touches → survivor
    write_manifest(tmp_path, [{
        "id": "uncaught",
        "file": "mod.py",
        "find": 'return "spare"',
        "replace": 'return "mutated"',
        "tests": ["test_mod.py::test_add"],
    }])
    before = (tmp_path / "mod.py").read_bytes()
    res = run_mutprobe(tmp_path)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "GREEN" in res.stdout
    assert "test-ga" in res.stdout or "gap" in res.stdout
    assert (tmp_path / "mod.py").read_bytes() == before


# ------------------------------------------------------------- safety rails


def test_ambiguous_find_is_rejected_without_touching_the_file(tmp_path):
    # two identical `return a + b` lines → the find-string is ambiguous
    make_project(tmp_path, mod_src=MOD_SRC + textwrap.dedent("""\


        def add2(a, b):
            return a + b
        """))
    write_manifest(tmp_path, [PROBE_ADD])
    before = (tmp_path / "mod.py").read_bytes()
    res = run_mutprobe(tmp_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "occurs 2 times" in res.stderr
    assert (tmp_path / "mod.py").read_bytes() == before


def _git(tmp_path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c",
         "user.name=t", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, timeout=60,
    )


def test_dirty_target_refused_then_allow_dirty_restores_dirty_bytes(tmp_path):
    make_project(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "mod.py", "test_mod.py")
    _git(tmp_path, "commit", "-qm", "base")
    # uncommitted edit on the TARGET file
    dirty = MOD_SRC + "# uncommitted work\n"
    (tmp_path / "mod.py").write_text(dirty)
    write_manifest(tmp_path, [PROBE_ADD])

    res = run_mutprobe(tmp_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "REFUSING" in res.stderr
    assert "--allow-dirty" in res.stderr
    assert (tmp_path / "mod.py").read_text() == dirty  # untouched

    res = run_mutprobe(tmp_path, "--allow-dirty")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "July 2026" in res.stderr  # the loud clobber-incident warning
    # THE point: restore is byte-snapshot, never `git checkout` — the
    # uncommitted edit must survive the probe run
    assert (tmp_path / "mod.py").read_text() == dirty


def test_operational_failure_still_restores_and_exits_one(tmp_path):
    make_project(tmp_path)
    # selector names a test that doesn't exist → pytest usage error (exit 4)
    write_manifest(tmp_path, [{
        "id": "bad-selector",
        "file": "mod.py",
        "find": "return a + b",
        "replace": "return a - b",
        "tests": ["test_mod.py::test_nope"],
    }])
    before = (tmp_path / "mod.py").read_bytes()
    res = run_mutprobe(tmp_path)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "ERROR" in res.stdout
    assert (tmp_path / "mod.py").read_bytes() == before


def test_interrupt_mid_pytest_restores_before_exit(tmp_path):
    """A probe whose pytest run SIGINTs the mutprobe process itself (the
    parent of the pytest subprocess) — simulating a user Ctrl-C mid-run. The
    mutated file must be restored byte-exact before mutprobe dies."""
    make_project(tmp_path, test_src=textwrap.dedent("""\
        import os, signal, time
        from mod import add

        def test_add():
            os.kill(os.getppid(), signal.SIGINT)  # ppid == mutprobe
            time.sleep(30)
        """))
    write_manifest(tmp_path, [PROBE_ADD])
    before = (tmp_path / "mod.py").read_bytes()
    res = run_mutprobe(tmp_path, timeout=60)
    assert res.returncode != 0, res.stdout + res.stderr
    assert (tmp_path / "mod.py").read_bytes() == before
    assert "restored" in (res.stdout + res.stderr).lower()


# ------------------------------------------------------------------ doctor


def test_doctor_green_on_valid_manifest(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path, [PROBE_ADD])
    res = run_mutprobe(tmp_path, "--doctor")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 problem(s)" in res.stdout
    assert (tmp_path / "mod.py").read_text() == MOD_SRC  # doctor never mutates


def test_doctor_flags_missing_file_bad_find_and_empty_collection(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path, [
        {"id": "no-such-file", "file": "gone.py", "find": "x", "replace": "y",
         "tests": ["test_mod.py::test_add"]},
        {"id": "find-missing", "file": "mod.py", "find": "not in the file",
         "replace": "y", "tests": ["test_mod.py::test_add"]},
        {"id": "collects-nothing", "file": "mod.py", "find": "return a + b",
         "replace": "return a - b", "tests": ["test_mod.py::test_nope"]},
    ])
    res = run_mutprobe(tmp_path, "--doctor")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "3 problem(s)" in res.stdout


def test_probe_flag_selects_one_and_unknown_id_errors(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path, [PROBE_ADD])
    res = run_mutprobe(tmp_path, "--probe", "flip-add")
    assert res.returncode == 0, res.stdout + res.stderr
    res = run_mutprobe(tmp_path, "--probe", "nope")
    assert res.returncode == 1
    assert "no probe with id" in res.stderr
