#!/usr/bin/env python3
"""Synthetic "external drives" for tests: hdiutil-backed DMG volumes.

macOS mounts a DMG as a real volume with a REAL diskutil VolumeUUID that
survives detach/re-attach — the identity a volume-aware app keys on.
That makes drive-lifecycle behaviour
(index -> unplug -> offline browse -> replug -> reindex) testable on one
machine with no physical drives. Stdlib only; importable as a module
(tests/test_fakedrive_lifecycle.py) and runnable as a CLI.

Artifacts
---------
Every DMG plus the state file live in ONE base directory, resolved as:
  1. the ``dir=`` argument / ``--dir`` flag, else
  2. ``$FAKEDRIVE_HOME``, else
  3. ``~/.fakedrives``
The state file (``fakedrives.json``) maps drive name -> dmg path, so a
crashed run can always be cleaned up later with ``fakedrive.py destroy <name>``
(or ``list`` to see what's left).

Safety (constitutional, not best-effort)
----------------------------------------
This tool must be incapable of touching a real volume:
  * every detach target is resolved through ``hdiutil info``: the target must
    be backed by a disk image whose path is a ``*.dmg`` INSIDE the base
    directory (`_assert_owned_dmg`). Real volumes are not backed by such an
    image, so pointing the tool at one is refused with an error, never forced.
  * the only paths it ever unlinks are that owned ``*.dmg`` and its own state
    file. Both unlink sites go through `_assert_owned_dmg` / `_state_path`.
  * it never writes inside any mounted volume itself — callers put test files
    on the mounts; this tool only creates/attaches/detaches/deletes its DMGs.

CLI
---
  fakedrive.py create --name X [--size-mb 64] [--fs APFS] [--dir D] [--json]
  fakedrive.py list [--dir D] [--json]
  fakedrive.py unmount <name|mount-point> [--dir D] [--json]
  fakedrive.py remount <name> [--dir D] [--json]
  fakedrive.py destroy <name> [--dir D] [--json]     # idempotent
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "fakedrives.json"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,26}")


class FakeDriveError(Exception):
    """Refusal or failure; the message is meant for humans."""


def default_base_dir() -> Path:
    env = os.environ.get("FAKEDRIVE_HOME")
    return Path(env).expanduser() if env else Path("~/.fakedrives").expanduser()


def _resolve_base(dir: str | Path | None) -> Path:
    return (Path(dir).expanduser() if dir else default_base_dir()).resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# subprocess / plist helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise FakeDriveError(f"required tool missing: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise FakeDriveError(f"timed out after {timeout}s: {' '.join(cmd)}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise FakeDriveError(f"{' '.join(cmd)} failed (rc={proc.returncode}): {err}")
    return proc


def _hdiutil_images() -> list[dict]:
    """All currently-attached disk images, per `hdiutil info -plist`."""
    out = _run(["hdiutil", "info", "-plist"]).stdout
    try:
        info = plistlib.loads(out)
    except plistlib.InvalidFileException as e:
        raise FakeDriveError("could not parse `hdiutil info -plist` output") from e
    return info.get("images", [])


def _image_for_dmg(dmg_path: Path) -> dict | None:
    """The attached-image record backed by `dmg_path`, or None if detached."""
    want = os.path.realpath(dmg_path)
    for img in _hdiutil_images():
        got = img.get("image-path")
        if got and os.path.realpath(got) == want:
            return img
    return None


def _entities(img: dict) -> list[dict]:
    return [e for e in img.get("system-entities", []) if e.get("dev-entry")]


def _mount_point_of(img: dict) -> str | None:
    for e in _entities(img):
        if e.get("mount-point"):
            return e["mount-point"]
    return None


def _volume_uuid(mount_point: str) -> str | None:
    out = _run(["diskutil", "info", "-plist", mount_point]).stdout
    try:
        info = plistlib.loads(out)
    except plistlib.InvalidFileException:
        return None
    return info.get("VolumeUUID") or info.get("DiskUUID")


# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------

def _state_path(base: Path) -> Path:
    return base / STATE_FILENAME


def _load_state(base: Path) -> dict[str, dict]:
    p = _state_path(base)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise FakeDriveError(f"corrupt state file {p}: {e}") from e
    return data if isinstance(data, dict) else {}


def _save_state(base: Path, state: dict[str, dict]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(base).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, _state_path(base))


# ---------------------------------------------------------------------------
# THE safety chokepoint
# ---------------------------------------------------------------------------

def _assert_owned_dmg(dmg_path: str | Path, base: Path) -> Path:
    """Refuse unless `dmg_path` is a *.dmg inside OUR base directory.

    Every detach and every unlink in this module goes through here first.
    A real volume can never satisfy this (it is not backed by a dmg in our
    directory), so the tool is structurally incapable of ejecting or deleting
    anything it did not create.
    """
    p = Path(os.path.realpath(Path(dmg_path).expanduser()))
    base = Path(os.path.realpath(base))
    if p.suffix != ".dmg" or p.parent != base:
        raise FakeDriveError(
            f"refusing to touch {dmg_path!s}: not a .dmg inside the fakedrive "
            f"base directory {base} — this tool only ever detaches/deletes "
            f"volumes it created itself"
        )
    return p


def _detach_image(dmg_path: Path, base: Path) -> None:
    """Detach the image backed by our dmg (no-op if not attached)."""
    _assert_owned_dmg(dmg_path, base)
    img = _image_for_dmg(dmg_path)
    if img is None:
        return
    devs = [e["dev-entry"] for e in _entities(img)]
    if not devs:
        raise FakeDriveError(f"attached image {dmg_path} exposes no dev entries")
    # The base whole-disk of the image is attached first -> lowest/shortest
    # dev node. Detaching it ejects the container and any synthesized (APFS)
    # disks with it.
    base_dev = min(devs, key=lambda d: (len(d), d))
    try:
        _run(["hdiutil", "detach", base_dev, "-quiet"])
    except FakeDriveError:
        time.sleep(1)
        _run(["hdiutil", "detach", base_dev, "-force", "-quiet"])
    # Verify: the image must be fully gone; mop up stragglers if not.
    left = _image_for_dmg(dmg_path)
    if left is not None:
        for dev in [e["dev-entry"] for e in _entities(left)]:
            try:
                _run(["hdiutil", "detach", dev, "-force", "-quiet"])
            except FakeDriveError:
                pass
        if _image_for_dmg(dmg_path) is not None:
            raise FakeDriveError(
                f"could not fully detach {dmg_path}; run "
                f"`hdiutil detach {base_dev} -force` manually"
            )


def _attach_image(dmg_path: Path, base: Path) -> tuple[str, str]:
    """Attach our dmg; return (mount_point, volume_uuid)."""
    _assert_owned_dmg(dmg_path, base)
    out = _run(["hdiutil", "attach", "-plist", "-nobrowse", str(dmg_path)]).stdout
    try:
        plist = plistlib.loads(out)
    except plistlib.InvalidFileException as e:
        raise FakeDriveError(f"could not parse attach output for {dmg_path}") from e
    mount = None
    for e in plist.get("system-entities", []):
        if e.get("mount-point"):
            mount = e["mount-point"]
            break
    if not mount:
        _detach_image(dmg_path, base)
        raise FakeDriveError(f"attach of {dmg_path} produced no mount point")
    uuid = _volume_uuid(mount)
    if not uuid:
        _detach_image(dmg_path, base)
        raise FakeDriveError(f"no VolumeUUID reported for {mount}")
    return mount, uuid


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

@dataclass
class FakeDrive:
    """One synthetic drive. Context-managed use guarantees cleanup:

        with fakedrive.create("PVFAKEA", dir=base) as d:
            ... use d.mount_point ...
        # detached + dmg deleted here, even on exceptions
    """

    name: str
    dmg_path: Path
    base_dir: Path
    mount_point: str | None = None
    volume_uuid: str | None = None
    _destroyed: bool = field(default=False, repr=False)

    # -- live status ------------------------------------------------------
    @property
    def is_attached(self) -> bool:
        return _image_for_dmg(self.dmg_path) is not None

    def refresh(self) -> "FakeDrive":
        img = _image_for_dmg(self.dmg_path)
        self.mount_point = _mount_point_of(img) if img else None
        if self.mount_point:
            self.volume_uuid = _volume_uuid(self.mount_point) or self.volume_uuid
        return self

    # -- lifecycle --------------------------------------------------------
    def unmount(self) -> "FakeDrive":
        """Detach (simulates unplugging). The dmg and its files survive."""
        _detach_image(self.dmg_path, self.base_dir)
        self.mount_point = None
        return self

    def remount(self) -> "FakeDrive":
        """Re-attach (simulates plugging back in). VolumeUUID is stable."""
        if not self.dmg_path.exists():
            raise FakeDriveError(f"dmg missing for {self.name!r}: {self.dmg_path}")
        img = _image_for_dmg(self.dmg_path)
        if img is not None:  # already attached
            self.mount_point = _mount_point_of(img)
            if self.mount_point:
                self.volume_uuid = _volume_uuid(self.mount_point) or self.volume_uuid
            return self
        self.mount_point, self.volume_uuid = _attach_image(self.dmg_path, self.base_dir)
        return self

    def destroy(self) -> "FakeDrive":
        """Detach if attached, delete the dmg, forget it. Idempotent."""
        if self._destroyed and not self.dmg_path.exists():
            return self
        _detach_image(self.dmg_path, self.base_dir)  # no-op when detached
        if self.dmg_path.exists():
            os.unlink(_assert_owned_dmg(self.dmg_path, self.base_dir))
        state = _load_state(self.base_dir)
        if self.name in state:
            del state[self.name]
            _save_state(self.base_dir, state)
        self.mount_point = None
        self._destroyed = True
        return self

    # -- context manager --------------------------------------------------
    def __enter__(self) -> "FakeDrive":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.destroy()

    # -- reporting --------------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "dmg": str(self.dmg_path),
            "dmg_exists": self.dmg_path.exists(),
            "mount_point": self.mount_point,
            "volume_uuid": self.volume_uuid,
        }


def create(name: str, size_mb: int = 64, fs: str = "APFS",
           dir: str | Path | None = None) -> FakeDrive:
    """Create a DMG named `name` and attach it. Returns a mounted FakeDrive."""
    if not NAME_RE.fullmatch(name or ""):
        raise FakeDriveError(
            f"bad drive name {name!r}: use 1-27 chars of [A-Za-z0-9._-], "
            f"starting alphanumeric"
        )
    if size_mb < 8 or size_mb > 4096:
        raise FakeDriveError(f"size_mb {size_mb} out of sane range 8..4096")
    base = _resolve_base(dir)
    base.mkdir(parents=True, exist_ok=True)
    state = _load_state(base)
    dmg = base / f"{name}.dmg"
    if name in state or dmg.exists():
        raise FakeDriveError(
            f"fake drive {name!r} already exists in {base} "
            f"(destroy it first: fakedrive.py destroy {name})"
        )
    _run([
        "hdiutil", "create", "-size", f"{size_mb}m", "-fs", fs,
        "-volname", name, "-quiet", str(dmg),
    ])
    state[name] = {"dmg": str(dmg), "fs": fs, "size_mb": size_mb,
                   "created_at": _now()}
    _save_state(base, state)  # recorded BEFORE attach: crash here stays cleanable
    drive = FakeDrive(name=name, dmg_path=dmg, base_dir=base)
    try:
        drive.mount_point, drive.volume_uuid = _attach_image(dmg, base)
    except BaseException:
        drive.destroy()
        raise
    return drive


def get(name: str, dir: str | Path | None = None) -> FakeDrive:
    """A FakeDrive handle for a state-recorded drive (attach state refreshed)."""
    base = _resolve_base(dir)
    state = _load_state(base)
    if name not in state:
        raise FakeDriveError(f"unknown fake drive {name!r} (state: {_state_path(base)})")
    dmg = _assert_owned_dmg(state[name]["dmg"], base)
    return FakeDrive(name=name, dmg_path=dmg, base_dir=base).refresh()


def list_drives(dir: str | Path | None = None) -> list[dict]:
    base = _resolve_base(dir)
    out = []
    for name in sorted(_load_state(base)):
        try:
            out.append(get(name, base).as_dict())
        except FakeDriveError as e:
            out.append({"name": name, "error": str(e)})
    return out


def unmount_target(target: str, dir: str | Path | None = None) -> FakeDrive:
    """Unmount by drive name or by mount point. Refuses foreign targets."""
    base = _resolve_base(dir)
    state = _load_state(base)
    if target in state:
        return get(target, base).unmount()
    # Treat as a mount point: it must resolve, via hdiutil, to an image WE own.
    want = os.path.realpath(target)
    for img in _hdiutil_images():
        mp = _mount_point_of(img)
        if mp and os.path.realpath(mp) == want:
            dmg = Path(img.get("image-path", ""))
            _assert_owned_dmg(dmg, base)  # raises for foreign images
            name = dmg.stem
            d = FakeDrive(name=name, dmg_path=dmg, base_dir=base)
            return d.unmount()
    raise FakeDriveError(
        f"refusing to unmount {target!r}: it is not a fake drive name in "
        f"{_state_path(base)} and not a mount backed by a disk image in {base}"
    )


def destroy(name: str, dir: str | Path | None = None) -> dict:
    """Destroy by name. Idempotent: unknown/already-gone is success."""
    base = _resolve_base(dir)
    state = _load_state(base)
    entry = state.get(name)
    dmg = Path(entry["dmg"]) if entry else base / f"{name}.dmg"
    existed = dmg.exists() or entry is not None
    d = FakeDrive(name=name, dmg_path=dmg, base_dir=base)
    d.destroy()
    return {"name": name, "destroyed": existed, "dmg": str(dmg)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(payload: dict | list, as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else human)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fakedrive.py",
        description="Synthetic hdiutil-backed volumes for tests.",
    )
    ap.add_argument("--dir", help="base directory for dmgs + state "
                                  "(default: $FAKEDRIVE_HOME or ~/.fakedrives)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p):
        # Accept --dir/--json AFTER the subcommand too (the docstring's natural
        # spelling — dogfooding caught the mismatch). default=SUPPRESS means an
        # absent trailing flag leaves the leading value untouched, and a present
        # one overrides it; no namespace-clobbering subtleties.
        p.add_argument("--dir", default=argparse.SUPPRESS,
                       help="base directory for dmgs + state")
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="machine-readable output")
        return p

    p_create = _common(sub.add_parser("create", help="create + mount a fake drive"))
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--size-mb", type=int, default=64)
    p_create.add_argument("--fs", default="APFS")

    _common(sub.add_parser("list", help="list known fake drives"))
    p_un = _common(sub.add_parser("unmount", help="detach a fake drive (dmg kept)"))
    p_un.add_argument("target", help="drive name or mount point")
    p_re = _common(sub.add_parser("remount", help="re-attach a fake drive"))
    p_re.add_argument("name")
    p_de = _common(sub.add_parser("destroy", help="detach + delete the dmg (idempotent)"))
    p_de.add_argument("name")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "create":
            d = create(args.name, size_mb=args.size_mb, fs=args.fs, dir=args.dir)
            _emit(d.as_dict(), args.json,
                  f"created {d.name} ({args.size_mb} MB {args.fs}) at "
                  f"{d.mount_point} [uuid {d.volume_uuid}]")
        elif args.cmd == "list":
            drives = list_drives(args.dir)
            if args.json:
                _emit(drives, True, "")
            elif not drives:
                print("no fake drives")
            else:
                for d in drives:
                    if "error" in d:
                        print(f"{d['name']}: ERROR {d['error']}")
                    else:
                        mp = d["mount_point"] or "(unmounted)"
                        uu = d["volume_uuid"] or "-"
                        print(f"{d['name']}  {mp}  uuid={uu}  dmg={d['dmg']}")
        elif args.cmd == "unmount":
            d = unmount_target(args.target, dir=args.dir)
            _emit(d.as_dict(), args.json,
                  f"unmounted {d.name} (dmg kept: {d.dmg_path})")
        elif args.cmd == "remount":
            d = get(args.name, dir=args.dir).remount()
            _emit(d.as_dict(), args.json,
                  f"remounted {d.name} at {d.mount_point} [uuid {d.volume_uuid}]")
        elif args.cmd == "destroy":
            res = destroy(args.name, dir=args.dir)
            verb = "destroyed" if res["destroyed"] else "nothing to do for"
            _emit(res, args.json, f"{verb} {res['name']} ({res['dmg']})")
    except FakeDriveError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
