# ruff: noqa: F811  (pytest fixtures imported from test_core are redefined as parameters)
"""MCP connector tools are listed by name; a tool's input schema is fetched with mcp_describe."""
from __future__ import annotations

from test_core import FakeResp, _FakeConnectorManager, _tool_line, events, web  # noqa: F401

from arena import agent, connectors, permissions

SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "description": "What to echo"},
                                           "count": {"type": "integer"}}, "required": ["text"]}


def _tools(n):
    return {f"mcp__srv__tool{i}": {"server": "srv", "description": f"Tool number {i} does a thing.",
                                    "input_schema": SCHEMA} for i in range(n)}


def test_guide_lists_names_not_schemas():
    guide = connectors.ConnectorManager.guide(_tools(3))
    assert "mcp__srv__tool0: Tool number 0 does a thing." in guide
    assert '<tool name="mcp_describe">' in guide
    assert "What to echo" not in guide and "inputSchema" not in guide


def test_guide_never_drops_a_tool_silently():
    many = _tools(400)
    guide = connectors.ConnectorManager.guide(many)
    assert all(alias in guide for alias in many)          # every tool stays callable
    old_style = sum(len(alias) + len(str(item["input_schema"])) + 40 for alias, item in many.items())
    assert len(guide) < old_style / 2                     # far smaller than inlining every schema


def test_describe_gives_the_schema_and_suggests_names():
    tools = _tools(2)
    out = connectors.describe_tool(tools, "mcp__srv__tool1")
    assert "Tool number 1" in out and "What to echo" in out and '"required"' in out
    miss = connectors.describe_tool(tools, "mcp__srv__tool9")
    assert miss.startswith("No MCP tool named") and "mcp__srv__tool1" in miss


def test_mcp_describe_is_a_read_everywhere(tmp_path):
    assert all(permissions.decide(m, "mcp_describe") == "allow" for m in permissions.MODES)
    assert "mcp_describe" in agent.READ_ONLY_TOOLS
    tools = agent.LocalTools(tmp_path)
    tools.mcp_tools = _tools(1)
    ok, out = tools.run({"name": "mcp_describe", "params": {"name": "mcp__srv__tool0"}})
    assert ok and "What to echo" in out


def test_agent_describes_then_calls_an_mcp_tool(web, monkeypatch):
    appmod, c, cfg, use, sent = web
    cfg["permission_mode"] = "bypassPermissions"
    fake = _FakeConnectorManager(appmod, {"readOnlyHint": True})
    monkeypatch.setattr(appmod, "CONNECTORS", fake)
    appmod._ui_mode, appmod._ui_modality = "agent", "text"
    use(
        FakeResp([_tool_line('<tool name="mcp_describe"><name>mcp__demo__echo</name></tool>')]),
        FakeResp([_tool_line('<tool name="mcp__demo__echo"><arguments>{"text":"hi","count":2}</arguments></tool>')]),
        FakeResp(['a0:"Done"']),
    )
    events(c.post("/api/chat", json={"message": "echo it"}))
    first = sent[0][1]["userMessage"]["content"]
    assert "mcp__demo__echo" in first and '"required"' not in first   # listed, schema not inlined
    assert '"required"' in sent[1][1]["userMessage"]["content"]       # schema arrived on request
    assert fake.calls == [("mcp__demo__echo", {"text": "hi", "count": 2})]
