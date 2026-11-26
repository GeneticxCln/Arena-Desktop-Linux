"""The cache the app itself keeps, and one honest way to empty it.

Three of the worst bugs in this app were *stale state*, not broken code: a
saved Google account that outlived its session, a browser profile holding a
half-finished sign-in, and a models catalog remembered as empty. This module
lists exactly what the app caches, wipes it on request, and reports what it
touched.

Two rules are deliberate:

1. **Only the app's own cache.** Chats, saved accounts, settings, the workspace
   folder and ``cookies.txt`` are never touched here. Deleting a chat history
   to fix a stuck window would be a catastrophic way to fix a small problem.
2. **Never while it is in use.** Browser profiles belong to live processes;
   deleting one under a running browser is how the app used to crash. The
   caller passes ``keep`` for the profiles in use, and a profile that still
   cannot be removed is reported as skipped, not silently dropped.

What counts as cache:

======================  ==============================================
``browser``             ``<data>/google-browser`` and ``<data>/arena-browser``
                        -- the Chromium profiles the app opens, each with its
                        own cookie jar, sessions and HTTP cache
``catalog``             ``models_cache.json`` / ``elo_cache.json`` -- data
                        that is cheap to rebuild and dangerous when stale
``logs``                rotated ``desktop-*.log`` / crash dumps older than
                        the current run (the live log is never removed)
``tmp``                 the app's own temporary folder, if it created one
======================  ==============================================
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

LOG = logging.getLogger("arena.appcache")

# Browser profiles (Linux WebKitGTK storage plus legacy Chromium profile names).
BROWSER_PROFILES = ("webview", "google-browser", "arena-browser")
# Written by the app, cheap to rebuild, and the thing most likely to be stale.
CATALOG_FILES = ("models_cache.json", "elo_cache.json")
# Logs are cache in the practical sense: they are only read when something is
# already broken. The live one is excluded -- it is being written right now.
LOG_GLOBS = ("desktop-*.log", "desktop-crash-*.log", "desktop-trace-*.log")


def _size_of(path: Path) -> int:
    """Bytes under ``path``, without descending into symlinks or errors."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _live_log_paths() -> set[str]:
    """Log files the app is writing right now; unlinking one stops the log.

    ``arena.diag`` is the intended source, but this build can run without that
    module. The logging handlers cover the normal desktop case either way.
    """
    paths: set[str] = set()
    try:
        from arena import diag  # type: ignore

        for name in ("log_path", "trace_path"):
            getter = getattr(diag, name, None)
            if callable(getter):
                live = getter()
                if live:
                    paths.add(str(live).lower())
    except Exception:
        pass
    try:
        for logger in (logging.getLogger(), logging.getLogger("arena.desktop")):
            for handler in logger.handlers:
                name = getattr(handler, "baseFilename", None)
                if name:
                    paths.add(str(name).lower())
    except Exception:
        pass
    return paths


def _rmtree(path: Path) -> tuple[bool, str]:
    """Remove a tree; retry once for the file-handle case Windows loves.

    A browser that was only just closed keeps its profile files open for a
    moment. The first attempt fails with "being used by another process", the
    second usually succeeds. Returning the reason instead of raising is the
    point: one locked file must not abort the rest of the wipe.
    """
    for attempt in range(2):
        try:
            shutil.rmtree(path)
            return True, ""
        except FileNotFoundError:
            return True, ""
        except OSError as exc:
            if attempt == 0:
                _release_profile(path)
                time.sleep(0.6)
                continue
            return False, str(exc)
    return False, ""


# Two clears at once would fight over the same folders: the second one deletes
# what the first is still walking. The UI can ask from two places (settings and
# the boot screen), so the lock lives here rather than in either caller.
_CLEAR_LOCK = threading.Lock()


def _release_profile(path: Path) -> None:
    """Close browsers still holding this profile, where that is possible.

    Deliberately narrow: it looks for the profile path on the command line of
    running processes and kills only those. Killing a person's own Chrome --
    the one with their tabs in it -- to clear the app's cache would be a far
    worse outcome than a stale cache.
    """
    try:                                    # pragma: no cover - platform path
        import psutil                       # type: ignore
    except Exception:
        return
    target = str(path).lower()
    try:                                    # pragma: no cover - platform path
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            except Exception:
                continue
            if target and target in cmdline:
                try:
                    proc.terminate()
                except Exception:
                    pass
        time.sleep(0.4)
    except Exception:
        LOG.info("appcache: could not look for processes holding %s", path)


def plan(data_dir: Path) -> list[dict]:
    """What *would* be cleared, with sizes. Pure: touches nothing."""
    data_dir = Path(data_dir)
    items: list[dict] = []
    for name in BROWSER_PROFILES:
        folder = data_dir / name
        if folder.exists():
            items.append({"id": name, "kind": "browser-profile",
                          "label": f"browser profile: {name}",
                          "path": str(folder), "bytes": _size_of(folder)})
    for name in CATALOG_FILES:
        file = data_dir / name
        if file.exists():
            items.append({"id": name, "kind": "catalog", "label": name,
                          "path": str(file), "bytes": _size_of(file)})
    logs = []
    for pattern in LOG_GLOBS:
        for file in data_dir.glob(pattern):
            logs.append(file)
    if logs:
        items.append({"id": "logs", "kind": "logs",
                      "label": f"{len(logs)} old log file(s)",
                      "path": str(data_dir),
                      "bytes": sum(_size_of(f) for f in logs)})
    tmp = data_dir / "tmp"
    if tmp.exists():
        items.append({"id": "tmp", "kind": "tmp", "label": "temporary files",
                      "path": str(tmp), "bytes": _size_of(tmp)})
    return items


def clear(data_dir: Path, *, keep: tuple[str, ...] = ()) -> dict:
    """Empty the app's cache. Returns what happened, never raises.

    ``keep`` lets the caller drop one item from the run -- the Google window
    sign-in uses it to wipe everything *except* the profile that is about to
    be used, which would otherwise be deleted from under it.
    """
    data_dir = Path(data_dir)
    if not _CLEAR_LOCK.acquire(blocking=False):
        # Honest answer instead of a second destructive pass.
        return {"removed": [], "skipped": [{"id": "all",
                                            "reason": "a clear is already running"}],
                "freed": 0,
                "busy": True}
    try:
        return _clear_locked(data_dir, keep)
    finally:
        _CLEAR_LOCK.release()


def _clear_locked(data_dir: Path, keep: tuple[str, ...]) -> dict:
    removed: list[str] = []
    skipped: list[dict] = []
    freed = 0

    for name in BROWSER_PROFILES:
        if name in keep:
            continue
        folder = data_dir / name
        if not folder.exists():
            continue
        size = _size_of(folder)
        ok, why = _rmtree(folder)
        if ok:
            removed.append(name)
            freed += size
        else:
            skipped.append({"id": name, "reason": why})

    for name in CATALOG_FILES:
        file = data_dir / name
        if not file.exists():
            continue
        size = _size_of(file)
        try:
            file.unlink()
            removed.append(name)
            freed += size
        except OSError as exc:
            skipped.append({"id": name, "reason": str(exc)})

    # The current run's log is being written right now. On Windows the unlink
    # would fail anyway; on Linux it would silently succeed and the app would
    # keep writing into a deleted file, which is worse -- the log just stops
    # for no visible reason.
    live = _live_log_paths()
    log_files = [f for pattern in LOG_GLOBS for f in data_dir.glob(pattern)
                 if str(f).lower() not in live]
    if log_files:
        gone = 0
        for file in log_files:
            try:
                size = _size_of(file)
                file.unlink()
                gone += 1
                freed += size
            except OSError:
                continue
        if gone:
            removed.append("logs")

    tmp = data_dir / "tmp"
    if tmp.exists():
        size = _size_of(tmp)
        ok, why = _rmtree(tmp)
        if ok:
            removed.append("tmp")
            freed += size
        else:
            skipped.append({"id": "tmp", "reason": why})

    LOG.info("appcache: cleared %s (%d bytes); skipped %s",
             ", ".join(removed) or "nothing", freed,
             ", ".join(item["id"] for item in skipped) or "nothing")
    return {"removed": removed, "skipped": skipped, "freed": freed}


def summary(data_dir: Path) -> dict:
    """The cache as the settings screen shows it: total size and parts."""
    items = plan(data_dir)
    return {"items": items, "bytes": sum(item["bytes"] for item in items)}
