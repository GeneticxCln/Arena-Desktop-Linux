# ruff: noqa: F811  (pytest fixtures imported from test_core are redefined as parameters)
"""render_video: HTML composition -> MP4, frame-exact, served only from the project folder."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest
from test_core import FakeResp, events, web  # noqa: F401  (web is a fixture)

from arena import browser_agent as ba
from arena import video_render as vr

CHROME = ba.find_chrome()
FFMPEG = shutil.which("ffmpeg")
live = pytest.mark.skipif(not (CHROME and FFMPEG and shutil.which("ffprobe")),
                          reason="needs Chrome, ffmpeg and ffprobe")


# -- validation and maths ---------------------------------------------------

def _page(root, rel="video/intro.html", body="<p>hi</p>"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<!doctype html><html><body>{body}</body></html>", encoding="utf-8")
    return path


def test_frame_count():
    assert vr.frame_count(2, 10) == 20
    assert vr.frame_count(1.05, 30) == 32  # rounded up: the last partial frame is kept


@pytest.mark.parametrize("kwargs, message", [
    ({"fps": 0}, "fps"), ({"fps": 61}, "fps"), ({"duration": 121}, "120"), ({"duration": 0}, "duration"),
    ({"width": 4000}, "1920"), ({"height": 2000}, "1080"),
])
def test_limits(tmp_path, kwargs, message):
    _page(tmp_path)
    args = {"path": "video/intro.html", "fps": 30, "duration": 2, "width": 1280, "height": 720}
    args.update(kwargs)
    with pytest.raises(vr.RenderError, match=message):
        vr.make_spec(tmp_path, **args)


def test_paths_must_be_html_inside_the_folder(tmp_path):
    _page(tmp_path)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(vr.RenderError, match="html"):
        vr.make_spec(tmp_path, path="notes.txt", duration=1)
    with pytest.raises(vr.RenderError, match="inside"):
        vr.make_spec(tmp_path, path="../outside.html", duration=1)
    with pytest.raises(vr.RenderError, match="not found"):
        vr.make_spec(tmp_path, path="video/missing.html", duration=1)


def test_output_name_and_location(tmp_path):
    _page(tmp_path)
    spec = vr.make_spec(tmp_path, path="video/intro.html", duration=1, out="../../My Video!")
    assert spec.output.parent == tmp_path / ".arena-artifacts" / "videos"
    assert spec.output.name == "My-Video.mp4"
    assert vr.make_spec(tmp_path, path="video/intro.html", duration=1).output.name == "intro.mp4"


def test_ffmpeg_command(tmp_path):
    _page(tmp_path)
    (tmp_path / "music.mp3").write_bytes(b"x")
    plain = vr.make_spec(tmp_path, path="video/intro.html", duration=2, fps=25)
    cmd = vr.ffmpeg_command(plain, tmp_path / "out.mp4")
    assert cmd[cmd.index("-framerate") + 1] == "25" and "image2pipe" in cmd
    assert "yuv420p" in cmd and "+faststart" in cmd and "-shortest" not in cmd
    with_audio = vr.make_spec(tmp_path, path="video/intro.html", duration=2, audio="music.mp3")
    cmd = vr.ffmpeg_command(with_audio, tmp_path / "out.mp4")
    assert str(tmp_path / "music.mp3") in cmd and "-shortest" in cmd


# -- the folder server ------------------------------------------------------

def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def test_folder_server_serves_only_the_folder(tmp_path):
    root = tmp_path / "project"
    _page(root, "index.html", "inside")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=hunter2", encoding="utf-8")
    (root / "leak.txt").symlink_to(secret)
    with vr.FolderServer(root) as server:
        assert _get(server.url_for("index.html"))[0] == 200
        assert b"inside" in _get(server.url_for("index.html"))[1]
        for bad in ("leak.txt", "../secret.txt", "%2e%2e/secret.txt", "nothing.html"):
            status, body = _get(server.base + bad)
            assert status == 404 and b"hunter2" not in body
        wrong_token = server.base.rsplit("/", 2)[0] + "/wrong-token/index.html"
        assert _get(wrong_token)[0] == 404


# -- live rendering ---------------------------------------------------------

RED_TO_BLUE = """<style>html,body{margin:0;height:100%}#s{height:100%;
animation:c 2s linear forwards}@keyframes c{from{background:rgb(255,0,0)}to{background:rgb(0,0,255)}}</style>
<div id="s"></div>"""

SEEKED = """<style>html,body{margin:0;height:100%}</style><script>
window.__duration = 2;
window.__seek = async (t) => { document.body.style.background = t < 1 ? 'rgb(0,255,0)' : 'rgb(255,255,0)'; };
</script>"""


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,nb_read_frames,width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _pixel(path, frame):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          f"select=eq(n\\,{frame}),scale=2:2", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(raw[:3])


@pytest.fixture
def browser(tmp_path):
    b = ba.ChromeBrowser(chrome_path=CHROME, profile_dir=tmp_path / "profile", headless=True)
    yield b
    b.shutdown()


@live
def test_css_animation_renders_frame_exact(browser, tmp_path):
    root = tmp_path / "project"
    _page(root, "video/fade.html", RED_TO_BLUE)
    spec = vr.make_spec(root, path="video/fade.html", duration=2, fps=10, width=320, height=180)
    result = vr.render(browser, spec, key="test-render")
    info = _probe(result["path"])
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert (video["width"], video["height"]) == (320, 180)
    assert int(video["nb_read_frames"]) == 20
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.15
    first, last = _pixel(result["path"], 0), _pixel(result["path"], 19)
    assert first[0] > 200 and first[2] < 60, first   # t = 0: red
    assert last[2] > 200 and last[0] < 60, last      # t = 1.9: blue
    assert "test-render" not in browser.tabs          # the render tab is closed afterwards


@live
def test_seek_function_and_page_duration(browser, tmp_path):
    root = tmp_path / "project"
    _page(root, "video/seek.html", SEEKED)
    spec = vr.make_spec(root, path="video/seek.html", fps=10, width=320, height=180)  # no duration given
    result = vr.render(browser, spec, key="seek")
    assert result["frames"] == 20 and abs(result["duration"] - 2.0) < 1e-6
    green, yellow = _pixel(result["path"], 0), _pixel(result["path"], 15)
    assert green[1] > 200 and green[0] < 60, green
    assert yellow[0] > 200 and yellow[1] > 200 and yellow[2] < 60, yellow


@live
def test_audio_is_mixed_and_cut_to_length(browser, tmp_path):
    root = tmp_path / "project"
    _page(root, "video/fade.html", RED_TO_BLUE)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                    str(root / "tone.wav")], check=True)
    spec = vr.make_spec(root, path="video/fade.html", duration=2, fps=10, width=320, height=180,
                        audio="tone.wav")
    info = _probe(vr.render(browser, spec, key="audio")["path"])
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.2


@live
def test_stop_aborts_and_leaves_no_file(browser, tmp_path):
    import threading
    root = tmp_path / "project"
    _page(root, "video/fade.html", RED_TO_BLUE)
    spec = vr.make_spec(root, path="video/fade.html", duration=2, fps=10, width=320, height=180)
    stop = threading.Event()
    stop.set()
    with pytest.raises(vr.RenderError, match="stopped"):
        vr.render(browser, spec, key="stop", stop_event=stop)
    assert not spec.output.exists()
    assert not list(spec.output.parent.glob("*.part*"))


@live
def test_agent_turn_renders_a_video(web, tmp_path, monkeypatch):
    appmod, c, cfg, use, sent = web
    ws = tmp_path / "ws"
    _page(ws, "video/fade.html", RED_TO_BLUE)
    cfg["workspace_dir"] = str(ws)
    cfg["permission_mode"] = "bypassPermissions"
    real = ba.ChromeBrowser(chrome_path=CHROME, profile_dir=tmp_path / "profile", headless=True)
    monkeypatch.setattr(ba, "get_browser", lambda headless=True: real)
    say = lambda text: FakeResp(["a0:" + json.dumps(text)])  # noqa: E731
    try:
        use(say('<tool name="render_video"><path>video/fade.html</path><fps>5</fps>'
                '<duration>1</duration><width>320</width><height>180</height></tool>'),
            say("Rendered."))
        ev = events(c.post("/api/chat", json={"message": "make the video", "mode": "agent",
                                              "modality": "text", "agent_backend": "local"}))
        result = [e for e in ev if e.get("type") == "tool_result" and e.get("tool") == "render_video"][0]
        assert result["ok"] is True
        assert (ws / ".arena-artifacts" / "videos" / "fade.mp4").is_file()
        assert "render_video" in sent[0][1]["userMessage"]["content"]   # the guide teaches it
        assert "fade.mp4" in sent[1][1]["userMessage"]["content"]
    finally:
        real.shutdown()
    assert os.path.getsize(ws / ".arena-artifacts" / "videos" / "fade.mp4") > 1000
