"""Skills loader: built-in, .codify and .arena layers, and the rules that keep project skills plain data."""
from __future__ import annotations

import os

import pytest

from arena import skills


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


SKILL = """---
name: tidy-up
description: Tidy one file without changing behaviour.
tools: read_file, edit_file, run_cmd
---
# Tidy up
Read the file, make the smallest edits, run the tests.
"""


def test_parse_reads_header_and_body():
    skill, problem = skills.parse_skill(SKILL, "fallback", "workspace")
    assert problem is None
    assert skill.name == "tidy-up"
    assert skill.description == "Tidy one file without changing behaviour."
    assert skill.tools == ("read_file", "edit_file", "run_cmd")
    assert skill.body.startswith("# Tidy up")
    assert skill.notes == ()


def test_description_falls_back_to_first_text_line():
    skill, _ = skills.parse_skill("# Heading\n\nDo the thing carefully.\n", "do-thing", "workspace")
    assert skill.name == "do-thing"
    assert skill.description == "Do the thing carefully."


@pytest.mark.parametrize(
    "text, reason",
    [
        ("", "empty"),
        ("---\nname: Bad Name\n---\nbody", "not a valid skill name"),
        ("---\nname: ok\n---\n   \n", "no instructions"),
        ("x" * (skills.MAX_SKILL_CHARS + 1), "limit"),
    ],
)
def test_parse_refuses_bad_files_with_a_reason(text, reason):
    skill, problem = skills.parse_skill(text, "fallback", "workspace")
    assert skill is None
    assert reason in problem


def test_unknown_tools_and_header_keys_are_reported_not_obeyed():
    text = "---\nname: x\ndescription: d\ntools: read_file, launch_missiles\ncolour: red\n---\nbody\n"
    skill, _ = skills.parse_skill(text, "x", "workspace")
    assert skill.tools == ("read_file",)
    assert any("launch_missiles" in n for n in skill.notes)
    assert any("colour" in n for n in skill.notes)


def test_codify_moves_header_maps_to_arena_tools():
    """Codify files say `moves:` with Codify names; they load unchanged."""
    text = "---\nname: x\ndescription: d\nmoves: search_code, run_command, todo, plan, write, recon\n---\nbody\n"
    skill, _ = skills.parse_skill(text, "x", "codify")
    assert skill.tools == ("search", "run_cmd", "set_todos", "propose_plan", "write_file")
    assert any("recon" in n for n in skill.notes)


def test_layers_builtin_then_codify_then_arena(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    _write(builtin, "alpha.md", "---\nname: alpha\ndescription: built-in alpha\n---\nbuilt-in body\n")
    _write(builtin, "beta.md", "---\nname: beta\ndescription: built-in beta\n---\nbuilt-in body\n")
    monkeypatch.setattr(skills, "BUILTIN_DIR", builtin)
    ws = tmp_path / "ws"
    _write(ws, ".codify/skills/alpha.md", "---\nname: alpha\ndescription: codify alpha\n---\ncodify body\n")
    _write(ws, ".codify/skills/gamma.md", "---\nname: gamma\ndescription: codify gamma\n---\ncodify body\n")
    _write(ws, ".arena/skills/alpha.md", "---\nname: alpha\ndescription: arena alpha\n---\narena body\n")

    found = skills.load_skills(ws)
    assert found.names() == ["alpha", "beta", "gamma"]
    assert found.get("alpha").description == "arena alpha"
    assert found.get("alpha").source == "workspace"
    assert found.get("gamma").source == "codify"
    assert found.get("beta").source == "built-in"
    assert "alpha" in found.shadows


def test_disabled_skills_are_not_offered(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    _write(builtin, "alpha.md", "---\nname: alpha\ndescription: a\n---\nbody\n")
    monkeypatch.setattr(skills, "BUILTIN_DIR", builtin)
    ws = tmp_path / "ws"
    _write(ws, ".arena/skills/alpha.md", "---\nname: alpha\ndescription: b\n---\nbody\n")
    found = skills.load_skills(ws, disabled=["alpha"])
    assert found.get("alpha") is None
    assert "alpha" not in found.menu()


def test_menu_lists_names_and_descriptions_never_bodies(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "BUILTIN_DIR", tmp_path / "none")
    ws = tmp_path / "ws"
    _write(ws, ".arena/skills/tidy-up.md", SKILL)
    menu = skills.load_skills(ws).menu()
    assert "tidy-up" in menu and "Tidy one file" in menu
    assert "smallest edits" not in menu


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlinked_skill_files_and_directories_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "BUILTIN_DIR", tmp_path / "none")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=hunter2\n", encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / ".arena/skills").mkdir(parents=True)
    (ws / ".arena/skills/notes.md").symlink_to(secret)
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere, "evil.md", "---\nname: evil\ndescription: e\n---\nbody\n")
    (ws / ".codify").mkdir()
    (ws / ".codify/skills").symlink_to(elsewhere, target_is_directory=True)

    found = skills.load_skills(ws)
    assert found.names() == []
    assert any("notes.md" in p and "refused" in p for p in found.problems)
    assert any(".codify/skills" in p and "refused" in p for p in found.problems)
    assert "hunter2" not in "".join(found.problems)


def test_skill_folder_reads_only_its_skill_md(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "BUILTIN_DIR", tmp_path / "none")
    ws = tmp_path / "ws"
    _write(ws, ".arena/skills/deploy/SKILL.md", "---\ndescription: ship it\n---\nsteps\n")
    _write(ws, ".arena/skills/deploy/run.sh", "rm -rf /\n")
    found = skills.load_skills(ws)
    assert found.get("deploy").description == "ship it"
    assert any("run.sh" in p and "never read or run" in p for p in found.problems)


def test_render_then_parse_round_trips():
    text = skills.render_skill("my-skill", "Does one thing.", ["read_file", "run_cmd"], "Step one.\nStep two.")
    skill, problem = skills.parse_skill(text, "ignored", "workspace")
    assert problem is None
    assert (skill.name, skill.description, skill.tools) == ("my-skill", "Does one thing.", ("read_file", "run_cmd"))
    assert skill.body == "Step one.\nStep two."


def test_every_builtin_skill_loads_cleanly():
    found, problems = skills.builtin_skills()
    assert problems == []
    assert len(found) >= 12
    for skill in found:
        assert skill.description and len(skill.description) <= skills.MAX_DESCRIPTION_CHARS


def test_skill_headers_know_every_agent_tool():
    """A skill may name any tool the agent has; the loader's list must not fall behind."""
    from arena import agent, permissions
    tools = (set(permissions.KIND) - {"write_project"}) | set(agent.READ_ONLY_TOOLS) | set(agent.BROWSER_TOOLS)
    assert tools <= skills.KNOWN_TOOLS, sorted(tools - skills.KNOWN_TOOLS)
