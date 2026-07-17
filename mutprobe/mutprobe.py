#!/usr/bin/env python
"""mutprobe — declared mutation probes with guaranteed byte-exact restore.

Turns this repo's hand-rolled critic practice ("would the tests actually fail
under a broken implementation?") into a declared, repeatable tool:

  * probes live in a JSON manifest: {id, file, find, replace, tests, note};
  * `find` must occur EXACTLY ONCE in the target file (ambiguous probes are
    worthless and are refused before anything is touched);
  * per probe: snapshot original bytes in memory AND to a temp backup file,
    apply the mutation, run only that probe's pytest selectors, record
    RED (tests caught it — expected) or GREEN (survivor — a test-gate gap),
    then restore from the byte snapshot — NEVER `git checkout`, which is the
    July-2026 incident this tool codifies against — verify the restored bytes
    hash-identical, and re-run the probe's tests once to confirm the green
    baseline.

Exit codes:
  0  every probe RED, every restore verified, every baseline green
  1  operational error (ambiguous find, dirty tree, pytest usage error,
     restore verify failure, non-green baseline, ...)
  2  at least one GREEN survivor (a test-gap finding) and no operational error

Usage:
  .venv/bin/python scripts/mutprobe.py [--manifest probes/probes.json]
                                       [--probe ID] [--allow-dirty]
                                       [--doctor] [--root DIR] [--python EXE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MANIFEST = "probes/probes.json"

CLOBBER_WARNING = """\
!! --allow-dirty: running mutation probes over UNCOMMITTED changes.
!! Remember July 2026: a hand-rolled probe was "restored" with `git checkout --`
!! and silently wiped uncommitted work. mutprobe restores from byte snapshots,
!! never from git — but on a dirty tree those snapshots are the ONLY copy of
!! your changes. If this process is SIGKILLed mid-probe, the mutated file stays
!! on disk (a .mutprobe-backup-* temp file will hold the original bytes)."""

# pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html)
PYTEST_TESTS_FAILED = 1
PYTEST_OK = 0


class ProbeError(Exception):
    """Operational error: the run cannot be trusted / must not proceed."""


@dataclass
class Probe:
    id: str
    file: str
    find: str
    replace: str
    tests: list[str]
    note: str = ""


@dataclass
class Result:
    probe: Probe
    outcome: str = "ERROR"      # RED | GREEN | ERROR
    detail: str = ""
    tests_run: str = "-"
    duration: float = 0.0
    restored: bool = False
    baseline_green: bool = False


# --------------------------------------------------------------- manifest


def load_manifest(path: Path) -> list[Probe]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ProbeError(f"manifest not found: {path}")
    except json.JSONDecodeError as e:
        raise ProbeError(f"manifest {path} is not valid JSON: {e}")
    if isinstance(raw, dict):
        raw = raw.get("probes", raw)
    if not isinstance(raw, list) or not raw:
        raise ProbeError(f"manifest {path} must be a non-empty list of probes")
    probes: list[Probe] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ProbeError(f"probe #{i} is not an object")
        missing = [k for k in ("id", "file", "find", "replace", "tests")
                   if k not in entry]
        if missing:
            raise ProbeError(f"probe #{i} missing keys: {', '.join(missing)}")
        p = Probe(
            id=str(entry["id"]),
            file=str(entry["file"]),
            find=str(entry["find"]),
            replace=str(entry["replace"]),
            tests=[str(t) for t in entry["tests"]],
            note=str(entry.get("note", "")),
        )
        if p.id in seen:
            raise ProbeError(f"duplicate probe id {p.id!r}")
        seen.add(p.id)
        if not p.find:
            raise ProbeError(f"probe {p.id}: empty find string")
        if p.find == p.replace:
            raise ProbeError(f"probe {p.id}: find == replace (not a mutation)")
        if not p.tests:
            raise ProbeError(f"probe {p.id}: no tests declared")
        probes.append(p)
    return probes


def resolve_target(root: Path, probe: Probe) -> Path:
    target = (root / probe.file).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ProbeError(f"probe {probe.id}: file {probe.file!r} escapes --root")
    if not target.is_file():
        raise ProbeError(f"probe {probe.id}: file not found: {probe.file}")
    return target


def check_find_unique(target: Path, probe: Probe) -> bytes:
    """Return the file's current bytes; refuse unless `find` occurs exactly once."""
    data = target.read_bytes()
    n = data.count(probe.find.encode("utf-8"))
    if n != 1:
        raise ProbeError(
            f"probe {probe.id}: find-string occurs {n} times in {probe.file} "
            "(must be exactly 1 — ambiguous probes are worthless)"
        )
    return data


# ------------------------------------------------------------- git rail


def git_dirty_targets(root: Path, files: list[str]) -> list[str] | None:
    """Lines of `git status --porcelain` for the target files.

    None means "not a git repo / git unavailable" — cleanliness can't be
    verified, which is a warning, not a refusal (the byte snapshot is the
    actual restore mechanism; git is never used to restore)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", *files],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


# ------------------------------------------------------------ snapshotting


class FileGuard:
    """Byte snapshot of one file: in memory + a temp backup file on disk.

    restore() writes the snapshot back and verifies the re-read bytes are
    hash-identical. git is deliberately never involved."""

    def __init__(self, target: Path):
        self.target = target
        self.original = target.read_bytes()
        st = target.stat()
        self.times_ns = (st.st_atime_ns, st.st_mtime_ns)
        self.sha = hashlib.sha256(self.original).hexdigest()
        fd, backup = tempfile.mkstemp(
            prefix=f".mutprobe-backup-{target.name}-", suffix=".bak")
        with os.fdopen(fd, "wb") as f:
            f.write(self.original)
            f.flush()
            os.fsync(f.fileno())
        self.backup = Path(backup)

    def restore(self) -> bool:
        """Write original bytes back; True iff the file verifies hash-identical."""
        try:
            with open(self.target, "wb") as f:
                f.write(self.original)
                f.flush()
                os.fsync(f.fileno())
            # restore timestamps too, so pre-existing (valid) .pyc caches and
            # mtime-keyed tooling see the file as fully unchanged
            os.utime(self.target, ns=self.times_ns)
        except OSError as e:
            print(f"!! restore of {self.target} FAILED: {e}\n"
                  f"!! original bytes preserved at {self.backup}", file=sys.stderr)
            return False
        ok = hashlib.sha256(self.target.read_bytes()).hexdigest() == self.sha
        if ok:
            try:
                self.backup.unlink()
            except OSError:
                pass
        else:
            print(f"!! restored {self.target} does NOT verify; original bytes "
                  f"preserved at {self.backup}", file=sys.stderr)
        return ok


# ---------------------------------------------------------------- pytest


def default_python(root: Path) -> str:
    venv = root / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def run_pytest(python: str, root: Path, selectors: list[str],
               extra: list[str], timeout: int) -> subprocess.CompletedProcess:
    # PYTHONDONTWRITEBYTECODE: a probe run must never persist bytecode compiled
    # from MUTATED source. A same-size mutation restored within the same
    # integer second would otherwise leave a .pyc that still stat-validates
    # against the restored file — and silently runs the mutant afterwards.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [python, "-m", "pytest", *selectors, "-q", "--no-header", *extra]
    return subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout,
        env=env)


_COUNT_RE = re.compile(
    r"(\d+) (failed|passed|errors?|skipped|deselected|xfailed|xpassed)")


def summarize_counts(output: str) -> str:
    """Human summary ('1 failed, 2 passed') parsed from pytest's tail."""
    counts = _COUNT_RE.findall(output)
    if not counts:
        return "?"
    return ", ".join(f"{n} {word}" for n, word in counts)


# ------------------------------------------------------------------ doctor


def doctor(root: Path, probes: list[Probe], python: str, timeout: int) -> int:
    problems = 0
    for p in probes:
        issues: list[str] = []
        try:
            target = resolve_target(root, p)
            check_find_unique(target, p)
        except ProbeError as e:
            issues.append(str(e))
        try:
            res = run_pytest(python, root, p.tests, ["--collect-only"], timeout)
            n = len([ln for ln in res.stdout.splitlines() if "::" in ln])
            if res.returncode not in (0,) or n == 0:
                issues.append(
                    f"selectors collect no tests (pytest exit {res.returncode}): "
                    f"{' '.join(p.tests)}")
            else:
                collected = f"{n} test(s) collected"
        except (OSError, subprocess.TimeoutExpired) as e:
            issues.append(f"collect-only failed: {e}")
        if issues:
            problems += 1
            print(f"FAIL {p.id}")
            for msg in issues:
                print(f"     - {msg}")
        else:
            print(f"ok   {p.id}  ({p.file}; {collected})")
    print(f"\ndoctor: {len(probes)} probe(s), {problems} problem(s)")
    return 0 if problems == 0 else 1


# ------------------------------------------------------------------- run


def run_probe(root: Path, probe: Probe, python: str, timeout: int) -> Result:
    r = Result(probe=probe)
    t0 = time.monotonic()
    target = resolve_target(root, probe)
    original = check_find_unique(target, probe)
    guard = FileGuard(target)
    try:
        mutated = original.replace(
            probe.find.encode("utf-8"), probe.replace.encode("utf-8"), 1)
        target.write_bytes(mutated)
        try:
            res = run_pytest(python, root, probe.tests, ["-x"], timeout)
        except subprocess.TimeoutExpired:
            r.outcome, r.detail = "ERROR", f"pytest timed out after {timeout}s"
            return r
        r.tests_run = summarize_counts(res.stdout)
        if res.returncode == PYTEST_TESTS_FAILED:
            r.outcome = "RED"
        elif res.returncode == PYTEST_OK:
            r.outcome = "GREEN"
            r.detail = "mutation SURVIVED the declared tests (gate gap)"
        else:
            r.outcome = "ERROR"
            tail = (res.stdout.strip().splitlines() or ["<no output>"])[-1]
            r.detail = f"pytest exit {res.returncode}: {tail}"
        return r
    finally:
        r.restored = guard.restore()
        if r.restored and r.outcome in ("RED", "GREEN"):
            # post-restore: the probe's tests must be green again
            try:
                base = run_pytest(python, root, probe.tests, [], timeout)
                r.baseline_green = base.returncode == PYTEST_OK
                if not r.baseline_green:
                    r.detail = (r.detail + "; " if r.detail else "") + \
                        f"baseline NOT green after restore (exit {base.returncode})"
            except (OSError, subprocess.TimeoutExpired) as e:
                r.baseline_green = False
                r.detail = (r.detail + "; " if r.detail else "") + \
                    f"baseline re-run failed: {e}"
        r.duration = time.monotonic() - t0


def print_table(results: list[Result]) -> None:
    rows = [("id", "file", "result", "tests run", "duration")]
    for r in results:
        rows.append((r.probe.id, r.probe.file, r.outcome, r.tests_run,
                     f"{r.duration:.1f}s"))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    line = "  ".join("-" * w for w in widths)
    for j, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if j == 0:
            print(line)
    for r in results:
        if r.detail:
            print(f"  {r.probe.id}: {r.detail}")
        if not r.restored:
            print(f"  {r.probe.id}: RESTORE NOT VERIFIED for {r.probe.file}")


def run_all(root: Path, probes: list[Probe], python: str, timeout: int,
            allow_dirty: bool) -> int:
    files = sorted({p.file for p in probes})
    dirty = git_dirty_targets(root, files)
    if dirty is None:
        print("note: not a git repository (or git unavailable) — cannot verify "
              "target cleanliness; the byte snapshot is the only safety net.",
              file=sys.stderr)
    elif dirty:
        if not allow_dirty:
            print("REFUSING to run: target files have uncommitted changes:",
                  file=sys.stderr)
            for ln in dirty:
                print(f"  {ln}", file=sys.stderr)
            print("Commit or stash first, or re-run with --allow-dirty "
                  "(and read the warning it prints).", file=sys.stderr)
            return 1
        print(CLOBBER_WARNING, file=sys.stderr)

    results: list[Result] = []
    operational_error = False
    try:
        for p in probes:
            print(f"[{p.id}] mutating {p.file} → {' '.join(p.tests)}", flush=True)
            try:
                results.append(run_probe(root, p, python, timeout))
            except ProbeError as e:
                operational_error = True
                print(f"ERROR: {e}", file=sys.stderr)
                # raised before any mutation (bad path / ambiguous find):
                # nothing was touched, so there is nothing to restore
                results.append(Result(probe=p, outcome="ERROR", detail=str(e),
                                      restored=True))
    except (KeyboardInterrupt, Exception):
        # run_probe restores its own file in its finally; nothing here holds an
        # unrestored mutation. Surface the interrupt/loudly and exit non-zero.
        print("\ninterrupted — all touched files restored from byte snapshots.",
              file=sys.stderr)
        raise

    print()
    print_table(results)
    reds = sum(1 for r in results if r.outcome == "RED")
    greens = sum(1 for r in results if r.outcome == "GREEN")
    errors = sum(1 for r in results if r.outcome == "ERROR")
    unrestored = [r for r in results if not r.restored]
    stale_baseline = [r for r in results
                      if r.outcome in ("RED", "GREEN") and not r.baseline_green]
    print(f"\n{len(results)} probe(s): {reds} RED, {greens} GREEN, "
          f"{errors} ERROR; {len(unrestored)} restore failure(s), "
          f"{len(stale_baseline)} non-green baseline(s)")
    if errors or unrestored or stale_baseline or operational_error:
        return 1
    if greens:
        print("GREEN survivors above are test-gate gaps: the declared tests "
              "did not fail under a broken implementation.")
        return 2
    print("all probes RED; all files verified byte-identical after restore.")
    return 0


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mutprobe", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST,
                    help=f"probe manifest (default: {DEFAULT_MANIFEST})")
    ap.add_argument("--probe", help="run only the probe with this id")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run even if target files have uncommitted changes")
    ap.add_argument("--doctor", action="store_true",
                    help="validate the manifest (files, uniqueness, selectors)")
    ap.add_argument("--root", default=".",
                    help="repo root: files, git and pytest resolve here")
    ap.add_argument("--python", default=None,
                    help="python for pytest (default: <root>/.venv/bin/python)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-pytest-run timeout, seconds (default 600)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    python = args.python or default_python(root)
    try:
        probes = load_manifest(Path(args.manifest) if os.path.isabs(args.manifest)
                               else root / args.manifest)
        if args.probe:
            probes = [p for p in probes if p.id == args.probe]
            if not probes:
                raise ProbeError(f"no probe with id {args.probe!r}")
        if args.doctor:
            return doctor(root, probes, python, args.timeout)
        return run_all(root, probes, python, args.timeout, args.allow_dirty)
    except ProbeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
