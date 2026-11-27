# ruff: noqa: F811  (pytest fixtures imported from test_core are redefined as parameters)
"""Skills reach the agent: the use_skill tool, its permission, and real turns through /api/chat."""
from __future__ import annotations

import json

from test_core import FakeResp, events, web  # noqa: F401  (web is a fixture)

from arena import agent, permissions, providers, skills
from arena.providers.freebuff import FreebuffProvider

TIDY = """---
name: tidy-up
description: Tidy one file without changing behaviour.
tools: read_file, edit_file
---
# Tidy up
Read the file first, then make the smallest edits that keep behaviour identical.
"""


def _project(tmp_path):
    ws = tmp_path / "project"
    (ws / ".arena/skills").mkdir(parents=True)
    (ws / ".arena/skills/tidy-up.md").write_text(TIDY, encoding="utf-8")
    return ws


def _say(text: str) -> str:
    return "a0:" + json.dumps(text)


def test_use_skill_returns_the_body_and_refuses_unknown_names(tmp_path):
    ws = _project(tmp_path)
    tools = agent.LocalTools(ws, skills=skills.load_skills(ws))
    call = {"name": "use_skill", "params": {"name": "tidy-up"}}
    info = tools.describe(call)
    assert info["inside"] is True and info["summary"] == "Use skill tidy-up"
    ok, out = tools.run(call)
    assert ok and "smallest edits that keep behaviour identical" in out
    ok, out = tools.run({"name": "use_skill", "params": {"name": "nope"}})
    assert not ok and "tidy-up" in out


def test_use_skill_is_a_read_in_every_permission_mode():
    for mode in permissions.MODES:
        assert permissions.decide(mode, "use_skill") == "allow"
    assert "use_skill" in agent.READ_ONLY_TOOLS


def test_skills_guide_shows_the_menu_not_bodies(tmp_path):
    ws = _project(tmp_path)
    text = agent.skills_guide(skills.load_skills(ws))
    assert '<tool name="use_skill"><name>' in text
    assert "tidy-up" in text and "Tidy one file" in text
    assert "smallest edits" not in text
    assert agent.skills_guide(skills.EMPTY) == ""


def test_local_agent_turn_fetches_a_skill(web, tmp_path):
    appmod, c, cfg, use, sent = web
    ws = _project(tmp_path)
    cfg["workspace_dir"] = str(ws)
    use(
        FakeResp([_say('<tool name="use_skill"><name>tidy-up</name></tool>')]),
        FakeResp([_say("Tidied.")]),
    )
    ev = events(c.post("/api/chat", json={"message": "tidy main.py", "mode": "agent",
                                          "modality": "text", "agent_backend": "local"}))
    first = sent[0][1]["userMessage"]["content"]
    second = sent[1][1]["userMessage"]["content"]
    assert "tidy-up (project): Tidy one file" in first          # the menu rides with the guide
    assert "smallest edits that keep behaviour identical" not in first
    assert "smallest edits that keep behaviour identical" in second  # the body comes back as a tool result
    results = [e for e in ev if e.get("type") == "tool_result" and e.get("tool") == "use_skill"]
    assert results and results[0]["ok"] is True
    assert ev[-1]["text_a"].endswith("Tidied.")


def test_disabled_skill_is_neither_offered_nor_fetched(web, tmp_path):
    appmod, c, cfg, use, sent = web
    ws = _project(tmp_path)
    cfg["workspace_dir"] = str(ws)
    cfg["skills_disabled"] = ["tidy-up"]
    use(
        FakeResp([_say('<tool name="use_skill"><name>tidy-up</name></tool>')]),
        FakeResp([_say("ok")]),
    )
    ev = events(c.post("/api/chat", json={"message": "tidy", "mode": "agent",
                                          "modality": "text", "agent_backend": "local"}))
    assert "tidy-up (project):" not in sent[0][1]["userMessage"]["content"]
    assert "smallest edits" not in sent[1][1]["userMessage"]["content"]
    results = [e for e in ev if e.get("type") == "tool_result" and e.get("tool") == "use_skill"]
    assert results and results[0]["ok"] is False


def test_web_provider_tab_gets_the_menu_and_the_skill(web, tmp_path):
    appmod, c, cfg, use, sent = web
    ws = _project(tmp_path)
    seen: list[str] = []

    class _Stub(FreebuffProvider):
        def stream_turn(self, chat_id, message, history, **kwargs):
            seen.append(message)
            if len(seen) == 1:
                yield {"type": "text", "text": '<tool name="use_skill"><name>tidy-up</name></tool>'}
            else:
                yield {"type": "text", "text": "Provider tidied."}

    orig = providers.PROVIDERS["freebuff"]
    providers.PROVIDERS["freebuff"] = _Stub()
    try:
        ev = events(c.post("/api/chat", json={"message": "tidy it", "track": "freebuff",
                                              "model_id": "default", "workspace": str(ws)}))
    finally:
        providers.PROVIDERS["freebuff"] = orig
    assert len(seen) == 2
    assert "tidy-up (project): Tidy one file" in seen[0]
    assert "smallest edits that keep behaviour identical" in seen[1]
    assert any(e.get("type") == "tool_result" and e.get("tool") == "use_skill" and e.get("ok") for e in ev)


def test_skills_routes_list_toggle_and_create(web, tmp_path):
    appmod, c, cfg, use, sent = web
    ws = _project(tmp_path)
    cfg["workspace_dir"] = str(ws)

    listed = c.get("/api/skills").get_json()
    assert listed["folder"] == str(ws)
    by_name = {s["name"]: s for s in listed["skills"]}
    assert by_name["tidy-up"]["source"] == "workspace" and by_name["tidy-up"]["enabled"] is True
    assert by_name["prove"]["source"] == "built-in"

    off = c.post("/api/skills/toggle", json={"name": "prove", "enabled": False}).get_json()
    assert off["ok"] and cfg["skills_disabled"] == ["prove"]
    again = {s["name"]: s for s in c.get("/api/skills").get_json()["skills"]}
    assert again["prove"]["enabled"] is False  # still listed, so it can be switched back on
    assert "prove" not in appmod._skill_set(ws).names()
    c.post("/api/skills/toggle", json={"name": "prove", "enabled": True})
    assert cfg["skills_disabled"] == []

    made = c.post("/api/skills", json={"name": "release-notes", "description": "Write release notes.",
                                       "tools": ["read_file", "run_cmd"], "body": "Read git log, then write."})
    assert made.status_code == 200 and made.get_json()["ok"]
    path = ws / ".arena/skills/release-notes.md"
    assert path.is_file() and "tools: read_file, run_cmd" in path.read_text(encoding="utf-8")
    assert "release-notes" in appmod._skill_set(ws).names()

    dup = c.post("/api/skills", json={"name": "release-notes", "description": "x", "body": "y"})
    assert dup.status_code == 409
    bad = c.post("/api/skills", json={"name": "../escape", "description": "x", "body": "y"})
    assert bad.status_code == 400
    assert not (tmp_path / "escape.md").exists()
    empty = c.post("/api/skills", json={"name": "blank", "description": "x", "body": "  "})
    assert empty.status_code == 400
    bad_toggle = c.post("/api/skills/toggle", json={"name": "Not A Name", "enabled": False})
    assert bad_toggle.status_code == 400


def test_skill_menu_is_sent_once_then_only_when_it_changes(web, tmp_path):
    appmod, c, cfg, use, sent = web
    ws = _project(tmp_path)
    cfg["workspace_dir"] = str(ws)
    body = {"message": "one", "mode": "agent", "modality": "text", "agent_backend": "local"}
    use(FakeResp([_say("first")]))
    ev = events(c.post("/api/chat", json=body))
    chat_id = ev[0]["chat_id"]
    assert "tidy-up (project):" in sent[-1][1]["userMessage"]["content"]

    use(FakeResp([_say("second")]))
    events(c.post("/api/chat", json={**body, "message": "two", "chat_id": chat_id}))
    assert "tidy-up (project):" not in sent[-1][1]["userMessage"]["content"]

    (ws / ".arena/skills/release.md").write_text("---\ndescription: Cut a release.\n---\nsteps\n", encoding="utf-8")
    use(FakeResp([_say("third")]))
    events(c.post("/api/chat", json={**body, "message": "three", "chat_id": chat_id}))
    third = sent[-1][1]["userMessage"]["content"]
    assert "Skills available in this chat" in third and "release (project): Cut a release." in third
