"""Unit tests for the OpenCode local CLI provider."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from arena import providers
from arena.providers import opencode as oc_mod
from arena.providers.opencode import (
    OpenCodeProvider,
    _extract_session_id,
    _parse_opencode_json_line,
    _parse_opencode_models_output,
)
from arena.web import app as web_app


def test_opencode_provider_metadata_and_status(monkeypatch):
    oc = providers.get_provider("opencode")
    assert isinstance(oc, OpenCodeProvider)
    assert oc.id == "opencode"
    assert oc.kind == "local_agent"
    assert oc.badge == "O"
    assert oc.badge_class == "is-opencode"

    monkeypatch.setattr(oc_mod, "_find_opencode_cli", lambda: None)
    st_missing = oc.status()
    assert st_missing["available"] is False
    assert st_missing["signed_in"] is False
    assert oc.open_sign_in()["ok"] is False

    monkeypatch.setattr(oc_mod, "_find_opencode_cli", lambda: "/usr/local/bin/opencode")
    monkeypatch.setattr(oc, "_read_version", lambda p: "opencode v2.4.2")
    st_ready = oc.status()
    assert st_ready["available"] is True
    assert st_ready["signed_in"] is True
    assert "v2.4.2" in st_ready["detail"]
    assert oc.open_sign_in()["ok"] is True


def test_opencode_models_parser_handles_providers_and_caps():
    raw = "\n".join([
        "\x1b[32mbeatai/claude-opus-5-5\x1b[0m",
        "beatai/deepseek-v4.1-flash-free",
        "ollama/qwen3-vl:8b",
        "opencode/beam-free",
        "opencode/beam-free",  # duplicate skipped
        "not a model line",
        "",
    ])
    models = _parse_opencode_models_output(raw)
    assert [m["id"] for m in models] == [
        "beatai/claude-opus-5-5",
        "beatai/deepseek-v4.1-flash-free",
        "ollama/qwen3-vl:8b",
        "opencode/beam-free",
    ]
    assert models[0]["org"] == "Anthropic"
    assert models[0]["group"] == "beatai"
    assert "Reasoning" in models[0]["capabilities"]
    assert models[1]["org"] == "DeepSeek"
    assert models[1]["group"] == "beatai"
    assert "Fast" in models[1]["capabilities"]
    assert models[2]["org"] == "Alibaba"
    assert models[2]["group"] == "ollama"
    assert "Local" in models[2]["capabilities"]
    assert "Vision" in models[2]["capabilities"]
    assert models[3]["org"] == "OpenCode"
    assert models[3]["group"] == "opencode"

    # Config metadata enriches display name, context window, vision capability, and merges extra config models
    enriched = _parse_opencode_models_output(
        "beatai/deepseek-v4.1-flash-free\n",
        config_meta={
            "beatai/deepseek-v4.1-flash-free": {
                "name": "DeepSeek V4.1 Flash Free",
                "capabilities": {"input": ["text", "image"]},
                "limit": {"context": 200000},
            },
            "beatai/custom-model": {
                "name": "Custom Model",
                "capabilities": {},
                "limit": {"context": 128000},
            },
        },
    )
    assert [m["id"] for m in enriched] == ["beatai/deepseek-v4.1-flash-free", "beatai/custom-model"]
    assert enriched[0]["name"] == "DeepSeek V4.1 Flash Free (beatai)"
    assert "200k ctx" in enriched[0]["description"]
    assert "Vision" in enriched[0]["capabilities"]
    assert enriched[1]["name"] == "Custom Model (beatai)"


def test_opencode_json_line_parser_and_session_url():
    assert _extract_session_id("opencode://session/ses_abc123") == "ses_abc123"
    assert _extract_session_id("opencode://session/bad id!") == ""
    assert _extract_session_id("https://example.com") == ""

    sid, evs = _parse_opencode_json_line(
        json.dumps({"type": "text", "sessionID": "ses_12345", "part": {"type": "text", "text": "hello"}})
    )
    assert sid == "ses_12345"
    assert evs == [{"type": "text", "text": "hello"}]

    sid2, evs2 = _parse_opencode_json_line(
        json.dumps({"type": "reasoning", "sessionID": "ses_12345", "part": {"type": "reasoning", "text": "plan"}})
    )
    assert sid2 == "ses_12345"
    assert evs2 == [{"type": "reasoning", "text": "plan"}]

    _, tool_evs = _parse_opencode_json_line(
        json.dumps({
            "type": "tool_use",
            "sessionID": "ses_12345",
            "part": {"type": "tool", "tool": "bash", "state": {"title": "Run tests", "input": {"command": "pytest"}}},
        })
    )
    assert any(e["type"] == "tool" and e["tool"] == "bash" and "Run tests" in e["summary"] for e in tool_evs)

    _, err_evs = _parse_opencode_json_line(
        json.dumps({"type": "error", "error": {"data": {"message": "Rate limited"}}})
    )
    assert err_evs == [{"type": "error", "code": "opencode_cli", "message": "Rate limited"}]


def test_opencode_cli_stream_turn_session_resume_and_multistep(tmp_path: Path, monkeypatch):
    fake_cli = tmp_path / "fake_opencode.py"
    argv_log = tmp_path / "argv.json"
    fake_cli.write_text(
        f"""#!{sys.executable}
import json, pathlib, sys
args = sys.argv[1:]
pathlib.Path({str(argv_log)!r}).write_text(json.dumps(args), encoding="utf-8")
if "--session" in args and "ses_stale" in args:
    print(json.dumps({{"type": "error", "error": {{"message": "Session not found"}}}}), flush=True)
    sys.exit(1)
print(json.dumps({{"type": "step_start", "sessionID": "ses_live99", "part": {{"type": "step-start"}}}}), flush=True)
print(json.dumps({{"type": "reasoning", "sessionID": "ses_live99", "part": {{"type": "reasoning", "text": "thinking"}}}}), flush=True)
print(json.dumps({{"type": "text", "sessionID": "ses_live99", "part": {{"type": "text", "text": "Step one"}}}}), flush=True)
print(json.dumps({{"type": "step_start", "sessionID": "ses_live99", "part": {{"type": "step-start"}}}}), flush=True)
print(json.dumps({{"type": "text", "sessionID": "ses_live99", "part": {{"type": "text", "text": "Step two"}}}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    monkeypatch.setattr(oc_mod, "_find_opencode_cli", lambda: str(fake_cli))

    oc = OpenCodeProvider()

    # Turn 1: fresh session with --archived, --model, --auto, and multi-step separator
    evs1 = list(
        oc.stream_turn(
            "c1",
            "hello",
            [],
            workspace=str(tmp_path),
            model="opencode/beam-free",
            permission_mode="auto",
        )
    )
    args1 = json.loads(argv_log.read_text(encoding="utf-8"))
    assert "--archived" in args1
    assert "--auto" in args1
    assert "--model" in args1 and "opencode/beam-free" in args1
    assert any(e.get("type") == "meta_update" and e.get("external_url") == "opencode://session/ses_live99" for e in evs1)
    assert "".join(e["text"] for e in evs1 if e.get("type") == "text") == "Step one\n\nStep two"
    assert "".join(e["text"] for e in evs1 if e.get("type") == "reasoning") == "thinking"

    # Turn 2: stale session automatically falls back to a fresh session
    evs2 = list(
        oc.stream_turn(
            "c1",
            "follow up",
            [{"role": "user", "text": "hello"}],
            workspace=str(tmp_path),
            conversation_url="opencode://session/ses_stale",
        )
    )
    args2 = json.loads(argv_log.read_text(encoding="utf-8"))
    assert "--session" not in args2
    assert "--archived" in args2
    assert "Step one\n\nStep two" == "".join(e["text"] for e in evs2 if e.get("type") == "text")


def test_opencode_chat_route_persists_source_and_session(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "CHATS_DIR", tmp_path / "chats")

    class _StubOpenCode(OpenCodeProvider):
        def stream_turn(
            self,
            chat_id,
            message,
            history,
            *,
            memory_block="",
            workspace="",
            conversation_url="",
            model="",
            permission_mode="",
            is_followup_round=False,
            stop_event=None,
        ):
            yield {"type": "meta_update", "external_url": "opencode://session/ses_route77"}
            yield {"type": "text", "text": "OpenCode reply"}

    orig = providers.PROVIDERS["opencode"]
    providers.PROVIDERS["opencode"] = _StubOpenCode()
    try:
        client = web_app.app.test_client()
        resp = client.post(
            "/api/chat",
            json={"message": "hi opencode", "track": "opencode", "model_id": "opencode/beam-free"},
        )
        assert resp.status_code == 200
        events = [
            json.loads(line[5:].strip())
            for line in resp.get_data(as_text=True).splitlines()
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")
        ]
        cid = next(e["chat_id"] for e in events if e.get("chat_id"))
        saved = web_app._load_chat(cid)
        assert saved["source"] == "opencode"
        assert saved["external_url"] == "opencode://session/ses_route77"
        assert any("OpenCode reply" in str(m.get("text") or "") for m in saved.get("messages") or [])
    finally:
        providers.PROVIDERS["opencode"] = orig
