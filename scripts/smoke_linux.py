#!/usr/bin/env python3
"""Smoke-test a built Linux Arena Desktop binary.

Launches the binary, waits for the local Flask UI, reads the API token from the
served page and checks `/api/status` (the desktop shell and the arena.ai engine
must both be attached). Exits non-zero on failure; useful under xvfb-run in CI.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request


def _stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def main() -> int:
    binary = sys.argv[1] if len(sys.argv) > 1 else "dist/Arena-Desktop"
    port = int(os.environ.get("ARENA_SMOKE_PORT") or 43871)
    env = dict(os.environ, ARENA_PORT=str(port))
    proc = subprocess.Popen(
        [binary], env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        html = None
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                output = (proc.stdout.read() if proc.stdout else "")[-4000:]
                print(output, file=sys.stderr)
                raise SystemExit(f"binary exited with {proc.returncode} before serving the UI")
            try:
                html = urllib.request.urlopen(base + "/", timeout=2).read().decode("utf-8", "replace")
                break
            except Exception:
                time.sleep(0.5)
        if not html:
            raise SystemExit("local UI did not start within 90 seconds")

        match = re.search(r'name="api-token" content="([^"]+)"', html)
        if not match:
            raise SystemExit("served page has no API token meta tag")
        token = match.group(1)

        def fetch_status():
            req = urllib.request.Request(base + "/api/status", headers={"X-Arena-Token": token})
            return json.loads(urllib.request.urlopen(req, timeout=10).read())

        status = {}
        for _ in range(30):
            status = fetch_status()
            if status.get("desktop") and (status.get("engine") or {}).get("attached"):
                break
            time.sleep(1)

        engine = status.get("engine") or {}
        if not status.get("desktop"):
            raise SystemExit("desktop shell did not report itself attached")
        if not engine.get("attached"):
            raise SystemExit("arena.ai engine window did not attach")

        # `host` is filled in by the load-finished handler, which can land just
        # after the window attaches. Give it a moment, but stay offline-safe:
        # a runner without network still passes as long as the window attached.
        for _ in range(20):
            if engine.get("host"):
                break
            time.sleep(1)
            status = fetch_status()
            engine = status.get("engine") or {}

        note = "" if engine.get("host") else " (host not reported: engine still loading or offline)"
        print(f"smoke OK: UI {len(html)} bytes, desktop={status['desktop']}, "
              f"engine attached={engine['attached']}, host={engine.get('host')!r}{note}")
        return 0
    finally:
        _stop(proc)


if __name__ == "__main__":
    raise SystemExit(main())
