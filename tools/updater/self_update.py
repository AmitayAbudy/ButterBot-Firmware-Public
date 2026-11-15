"""
Dimension Hopper updates itself from the robot firmware's GitHub releases.

Every release carries DimensionHopper.exe and dimensionhopper.json ({"version": "1.4.0", "sha256": "..."}, written
by CI). check() compares that version with ours; apply() downloads the new exe next to the running one, checks its
SHA-256, then swaps the files - Windows won't overwrite a running exe, but it does let one be renamed - and starts
the new copy. The old file stays as "<name>.old.exe" until the new copy deletes it on its next start, and any failure
along the way puts the original back.

DH_UPDATE_FEED (a URL, file:// works) replaces the GitHub releases API for testing.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

EXE_ASSET = "DimensionHopper.exe"
MANIFEST_ASSET = "dimensionhopper.json"
CHECK_EVERY_S = 20 * 3600  # automatic checks at start-up reuse the last answer for this long
UA = {"User-Agent": "DimensionHopper-updater"}


class UpdateError(RuntimeError):
    pass


@dataclass
class Update:
    version: str
    tag: str
    page: str      # the release page ("What's new")
    url: str       # DimensionHopper.exe
    size: int
    sha256: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.strip().lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def newer(a: str, b: str) -> bool:
    """True when version a is newer than version b."""
    return parse_version(a) > parse_version(b)


def can_self_update() -> bool:
    """Only the packaged exe can swap itself; running from the .py sources, update with git instead."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def old_path(exe: Path) -> Path:
    return exe.with_name(exe.stem + ".old" + exe.suffix)


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers=dict(UA, Accept="application/vnd.github+json"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check(repo: str) -> Optional[Update]:
    """What the latest release offers, or None when it has no Dimension Hopper that says its version."""
    feed = os.environ.get("DH_UPDATE_FEED") or f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        rel = json.loads(_get(feed).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise UpdateError("GitHub rate limit hit - try again in a few minutes") from None
        if e.code == 404:
            return None
        raise UpdateError(f"GitHub said {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"couldn't reach GitHub ({getattr(e, 'reason', e)})") from None
    assets = {a.get("name", ""): a for a in rel.get("assets", [])}
    exe, man = assets.get(EXE_ASSET), assets.get(MANIFEST_ASSET)
    if not exe or not man:  # releases from before the updater existed
        return None
    try:
        info = json.loads(_get(man["browser_download_url"]).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"couldn't read the release's version file ({e})") from None
    sha = str(info.get("sha256", "")).lower()
    digest = str(exe.get("digest") or "")  # GitHub's own checksum of the upload, when it gives one
    if digest.startswith("sha256:") and sha and digest[7:].lower() != sha:
        raise UpdateError("the release's version file doesn't match its DimensionHopper.exe - "
                          "it's probably still being uploaded, try again in a few minutes")
    if not sha or not info.get("version"):
        return None
    return Update(str(info["version"]), rel.get("tag_name", ""), rel.get("html_url", ""),
                  exe["browser_download_url"], int(exe.get("size") or 0), sha)


def cached_check(cfg: dict, repo: str, force: bool = False) -> Optional[Update]:
    """check(), but an automatic check reuses the stored answer for CHECK_EVERY_S. Updates cfg (caller saves)."""
    if os.environ.get("DH_UPDATE_FEED"):  # a test feed: always ask it, never remember its answer
        return check(repo)
    last = cfg.get("self_update") or {}
    if not force and time.time() - float(last.get("checked", 0)) < CHECK_EVERY_S:
        try:
            u = last.get("update")
            return Update(**u) if u else None
        except TypeError:  # stored by a different version of this file
            pass
    u = check(repo)
    cfg["self_update"] = {"checked": time.time(), "update": u.as_dict() if u else None}
    return u


def cleanup_after_update() -> bool:
    """Run at start-up: deletes the previous version's leftover file. True if there was one (we were just updated)."""
    if not can_self_update():
        return False
    old = old_path(exe_path())
    if not old.exists():
        return False
    for _ in range(20):  # the old copy may still be closing
        try:
            old.unlink()
            break
        except OSError:
            time.sleep(0.25)
    return True


def writable(folder: Path) -> bool:
    probe = folder / f".dh-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def download(u: Update, on_progress: Callable[[int], None] = lambda p: None,
             cancel: Optional[threading.Event] = None) -> Path:
    """Fetches the new exe next to the running one (same drive, so the swap is a rename) and checks its SHA-256."""
    exe = exe_path()
    if not writable(exe.parent):
        raise UpdateError(f"can't write to {exe.parent} - download the new version from the release page instead")
    new = exe.with_name(exe.stem + ".new" + exe.suffix)
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(u.url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r, open(new, "wb") as f:
            total = int(r.headers.get("Content-Length") or u.size or 0)
            done = 0
            while chunk := r.read(256 * 1024):
                if cancel is not None and cancel.is_set():
                    raise UpdateError("cancelled")
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total:
                    on_progress(min(100, done * 100 // total))
    except UpdateError:
        new.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        new.unlink(missing_ok=True)
        raise UpdateError(f"download failed ({getattr(e, 'reason', e)})") from None
    if h.hexdigest() != u.sha256:
        new.unlink(missing_ok=True)
        raise UpdateError("the download doesn't match the release's checksum - nothing was changed, try again")
    return new


def install_and_restart(new: Path, args: list[str] | None = None) -> None:
    """Swaps the running exe for `new` and starts it. On success the caller must quit straight away.
    On failure the original is back in place and UpdateError says why."""
    exe = exe_path()
    old = old_path(exe)
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.replace(exe, old)  # allowed while running: Windows locks the file's contents, not its name
    except OSError as e:
        new.unlink(missing_ok=True)
        raise UpdateError(f"couldn't move the running copy aside ({e.strerror or e})") from None
    try:
        os.replace(new, exe)
    except OSError as e:
        os.replace(old, exe)
        new.unlink(missing_ok=True)
        raise UpdateError(f"couldn't put the new version in place ({e.strerror or e})") from None
    # A one-file exe started from another one would otherwise reuse the parent's unpacked files (which vanish when
    # the parent quits); this makes the new copy start fresh
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    env.pop("DH_PRETEND_VERSION", None)
    try:
        subprocess.Popen([str(exe), *(args or [])], env=env, cwd=str(exe.parent), close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as e:
        try:
            os.replace(exe, new)
            os.replace(old, exe)
        except OSError:
            pass
        raise UpdateError(f"couldn't start the new version ({e.strerror or e}) - kept the current one") from None
