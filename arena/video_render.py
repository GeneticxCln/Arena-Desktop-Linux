"""Render an HTML composition in the project to an MP4, frame by frame.

The idea comes from nexu-io/openmotion (Apache-2.0), whose renderer
(HyperFrames) treats a video as an animated HTML page. This is a fresh
implementation on Arena's own agent Chrome (``browser_agent``) and ffmpeg.

Nothing plays in real time. For each frame at time ``t`` the page is set to
exactly ``t`` and screenshotted:

1. ``window.__seek(t)`` if the page defines it (canvas, WebGL, custom code);
2. otherwise every ``document.getAnimations()`` entry (CSS animations and
   transitions, the Web Animations API) and GSAP's global timeline are
   paused and moved to ``t``;
3. ``<video>`` elements are paused and seeked to ``t`` in every case.

The page is served from the project folder by a private 127.0.0.1 server
with a random token in the path, never opened as ``file://``, so it cannot
read files outside the folder.
"""

from __future__ import annotations

import base64
import http.server
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

MAX_SECONDS = 120.0
MAX_FPS = 60
MAX_WIDTH, MAX_HEIGHT = 1920, 1080
OUTPUT_DIR = Path(".arena-artifacts") / "videos"


class RenderError(Exception):
    """A render failed; the message is written for the model to act on."""


@dataclass(frozen=True)
class RenderSpec:
    root: Path            # the folder that is served (the page can read only this)
    page: Path            # the HTML file, inside root
    fps: int
    width: int
    height: int
    output: Path
    duration: float | None = None   # None: read window.__duration from the page
    audio: Path | None = None


def frame_count(duration: float, fps: int) -> int:
    return max(1, math.ceil(round(duration * fps, 6)))


def _inside(root: Path, rel: str) -> Path:
    root = root.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise RenderError(f"{rel!r} is not inside the project folder.")
    return target


def make_spec(root, path: str, duration=None, fps=30, width=1280, height=720,
              audio: str | None = None, out: str | None = None) -> RenderSpec:
    """Check a render request and turn it into a spec, or raise RenderError."""
    root = Path(root).resolve()
    rel = str(path or "").strip()
    if not rel.lower().endswith((".html", ".htm")):
        raise RenderError("render_video needs an .html file (the composition).")
    page = _inside(root, rel)
    if not page.is_file():
        raise RenderError(f"{rel} was not found in the project folder.")
    try:
        fps = int(float(fps))
        width, height = int(float(width)), int(float(height))
        seconds = None if duration in (None, "") else float(duration)
    except (TypeError, ValueError) as exc:
        raise RenderError("fps, duration, width and height must be numbers.") from exc
    if not 1 <= fps <= MAX_FPS:
        raise RenderError(f"fps must be between 1 and {MAX_FPS}.")
    if seconds is not None and not 0 < seconds <= MAX_SECONDS:
        raise RenderError(f"duration must be more than 0 and at most {MAX_SECONDS:.0f} seconds.")
    if not (16 <= width <= MAX_WIDTH and 16 <= height <= MAX_HEIGHT):
        raise RenderError(f"The size must be at most {MAX_WIDTH} x {MAX_HEIGHT} (and at least 16 x 16).")
    sound = None
    if audio:
        sound = _inside(root, str(audio).strip())
        if not sound.is_file():
            raise RenderError(f"The audio file {audio} was not found in the project folder.")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(str(out or page.stem)).name).strip("-") or "video"
    return RenderSpec(root=root, page=page, fps=fps, width=width, height=height,
                      output=root / OUTPUT_DIR / f"{name[:80]}.mp4", duration=seconds, audio=sound)


def ffmpeg_command(spec: RenderSpec, target: Path, duration: float | None = None) -> list:
    """ffmpeg reading JPEG frames on stdin and writing a web-friendly H.264 MP4."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
           # The input codec must be named: ffmpeg cannot detect JPEG frames arriving on stdin.
           "-f", "image2pipe", "-framerate", str(spec.fps), "-c:v", "mjpeg", "-i", "-"]
    if spec.audio is not None:
        cmd += ["-i", str(spec.audio)]
    cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p"]
    if spec.audio is not None:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(target)]
    return cmd


class FolderServer:
    """Serves one folder on 127.0.0.1 under a random token; nothing outside it."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.token = secrets.token_urlsafe(18)
        root_dir, token = self.root, self.token

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802 - http.server API
                parts = urllib.parse.urlsplit(self.path).path.split("/", 2)
                if len(parts) < 3 or parts[1] != token:
                    return self.send_error(404)
                rel = urllib.parse.unquote(parts[2])
                target = (root_dir / rel).resolve()
                if (target != root_dir and root_dir not in target.parents) or not target.is_file():
                    return self.send_error(404)
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self._server.server_port}/{token}/"

    def url_for(self, page: Path | str) -> str:
        rel = Path(page).resolve().relative_to(self.root) if Path(page).is_absolute() else Path(page)
        return self.base + urllib.parse.quote(rel.as_posix())

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, name="video-folder-server", daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


# Moves the page to time t (seconds). Returns true once the frame is ready.
_SEEK_JS = r"""async (t) => {
  if (typeof window.__seek === 'function') {
    await window.__seek(t);
  } else {
    for (const a of document.getAnimations()) {
      try { a.pause(); a.currentTime = t * 1000; } catch (e) {}
    }
    if (window.gsap && window.gsap.globalTimeline) {
      window.gsap.globalTimeline.pause();
      window.gsap.globalTimeline.time(t, false);
    }
  }
  await Promise.all(Array.from(document.querySelectorAll('video')).map((v) => new Promise((done) => {
    v.pause();
    const want = Math.min(t, isFinite(v.duration) ? v.duration : t);
    if (Math.abs(v.currentTime - want) < 0.0005) return done();
    const timer = setTimeout(done, 3000);
    v.addEventListener('seeked', () => { clearTimeout(timer); done(); }, { once: true });
    v.currentTime = want;
  })));
  return true;
}"""

_READY_JS = r"""(async () => {
  if (document.fonts && document.fonts.ready) await document.fonts.ready;
  await Promise.all(Array.from(document.images).map((img) => img.complete ? 1
    : new Promise((done) => { img.addEventListener('load', done, { once: true });
                              img.addEventListener('error', done, { once: true }); })));
  for (const a of document.getAnimations()) { try { a.pause(); a.currentTime = 0; } catch (e) {} }
  if (window.gsap && window.gsap.globalTimeline) window.gsap.globalTimeline.pause();
  return typeof window.__duration === 'number' ? window.__duration : null;
})()"""


def render(browser, spec: RenderSpec, key: str = "render", stop_event=None) -> dict:
    """Render ``spec`` with the agent's Chrome; returns {path, frames, duration, fps, width, height}."""
    if not shutil.which("ffmpeg"):
        raise RenderError("ffmpeg is not installed, so videos cannot be rendered.")
    from arena import browser_agent

    spec.output.parent.mkdir(parents=True, exist_ok=True)
    part = spec.output.with_name(f".{spec.output.stem}.part-{secrets.token_hex(4)}.mp4")
    proc = None
    try:
        with FolderServer(spec.root) as server:
            tab = browser.tab(key)
            tab.open(server.url_for(spec.page))
            tab._send("Emulation.setDeviceMetricsOverride", {"width": spec.width, "height": spec.height,
                                                              "deviceScaleFactor": 1, "mobile": False})
            page_duration = tab._eval(_READY_JS, timeout=30.0)
            duration = spec.duration if spec.duration is not None else page_duration
            if not isinstance(duration, (int, float)) or not 0 < float(duration) <= MAX_SECONDS:
                raise RenderError("Give a duration, or set window.__duration (seconds) in the page, "
                                  f"at most {MAX_SECONDS:.0f}.")
            duration = float(duration)
            frames = frame_count(duration, spec.fps)
            proc = subprocess.Popen(ffmpeg_command(spec, part, frames / spec.fps), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            seek = tab._send("Runtime.evaluate", {"expression": f"({_SEEK_JS})", "objectGroup": "render"})
            seek_id = (seek.get("result") or {}).get("objectId")
            for index in range(frames):
                if stop_event is not None and stop_event.is_set():
                    raise RenderError("The render was stopped.")
                t = index / spec.fps
                tab._send("Runtime.callFunctionOn", {"objectId": seek_id, "functionDeclaration":
                                                     "function(t) { return this(t); }",
                                                     "arguments": [{"value": t}], "awaitPromise": True})
                shot = tab._send("Page.captureScreenshot", {"format": "jpeg", "quality": 95,
                                                             "captureBeyondViewport": False}, timeout=30.0)
                try:
                    proc.stdin.write(base64.b64decode(shot.get("data") or ""))
                except BrokenPipeError as exc:
                    raise RenderError("ffmpeg stopped early: " + _stderr(proc)) from exc
            proc.stdin.close()
            if proc.wait(timeout=120) != 0:
                raise RenderError("ffmpeg failed: " + _stderr(proc))
        os.replace(part, spec.output)
        return {"path": spec.output, "frames": frames, "duration": frames / spec.fps,
                "fps": spec.fps, "width": spec.width, "height": spec.height}
    except browser_agent.BrowserError as exc:
        raise RenderError(f"The page could not be rendered: {exc}") from exc
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if part.exists():
            part.unlink()
        try:
            browser.close_tab(key)
        except Exception:
            pass


def _stderr(proc) -> str:
    try:
        return (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[-400:] or "no details"
    except Exception:
        return "no details"
