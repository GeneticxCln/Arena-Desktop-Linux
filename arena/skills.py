"""Skills: written recipes an agent can pick up, kept as data rather than code.

Ported from Codify (https://github.com/GeneticxCln/Codify, ``engine/skills.py``,
MIT License, Copyright (c) 2026 Codify). The built-ins in ``arena/builtin_skills/``
are adapted from Codify's ``engine/builtin_skills/`` for Arena's tool names
(engineering-principles and plain-prose are unchanged). A skill is a name, a one-line
description and a body of instructions. The agent is shown the names and
descriptions in its tool guide and fetches a body with the ``use_skill`` tool
only when it wants one, so a long skill costs no context until it is used.

Where skills come from, later layers winning on a name clash:

1. ``arena/builtin_skills/`` shipped with the app (source "built-in");
2. ``<folder>/.codify/skills/``, so a project used in Codify shares its skills (source "codify");
3. ``<folder>/.arena/skills/`` (source "workspace").

A skill is either ``<name>.md`` or a ``<name>/SKILL.md`` folder.

## A skill is data, not a capability

Project skill files arrive with a cloned repository, so their text is untrusted.
Nothing here can change what the app is willing to do:

- a body is never executed, imported or evaluated; it is a string handed to the
  model as a tool result;
- a skill cannot add a tool. ``tools:`` in the header names the tools it is
  written around; it is a hint, never a grant. A name that is not a tool is
  dropped and reported;
- every tool a skill suggests still goes through the permission mode;
- symlinks are refused, and a skills directory must resolve to exactly
  ``<folder>/<dir>``, so a cloned repo cannot point a skill at ``~/.ssh/id_rsa``.

The worst a hostile skill can do is try to talk the agent into something it
could already do, under the same permission prompts.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_skills"

# (relative directory, source label), in loading order: later layers win.
WORKSPACE_LAYERS = ((".codify/skills", "codify"), (".arena/skills", "workspace"))
ARENA_SKILLS_DIR = ".arena/skills"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_CHARS = 32768
MAX_SKILL_BYTES = MAX_SKILL_CHARS * 4
MAX_DESCRIPTION_CHARS = 200
MAX_WORKSPACE_SKILLS = 100
MAX_SCANNED_ENTRIES = 2000
MAX_REPORTED_PROBLEMS = 25
MAX_EXTRAS_LISTED = 10
MAX_DECLARED_TOOLS = 24

# The header keys read. `moves` is Codify's name for `tools`.
HEADER_KEYS = ("name", "description", "tools", "moves")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_SHOWN = 40
_SHOWN_COUNT = 5

# The tools Arena's tool loop offers (see arena/agent.py GUIDE).
KNOWN_TOOLS = frozenset({
    "ls", "read_file", "read_files", "search", "write_file", "edit_file", "mkdir", "delete",
    "run_cmd", "ask_user", "propose_plan", "set_todos", "use_skill",
})

# Codify move names with an Arena tool of the same job. Codify moves missing here
# (recon, verify, git_history, recall, ...) have no Arena tool and are reported.
CODIFY_MOVES = {
    "search_code": "search",
    "run_command": "run_cmd",
    "todo": "set_todos",
    "plan": "propose_plan",
    "write": "write_file",
    "read_file": "read_file",
    "ask_user": "ask_user",
}


@dataclass(frozen=True)
class Skill:
    """One loaded skill. ``source`` is "built-in", "codify" or "workspace"."""

    name: str
    description: str
    body: str
    source: str
    # The tools the header says the skill is written around: a hint, never a grant.
    tools: tuple[str, ...] = ()
    # Header problems that did not stop the skill loading (unknown tool, unread key).
    notes: tuple[str, ...] = ()

    @property
    def is_builtin(self) -> bool:
        return self.source == "built-in"


@dataclass(frozen=True)
class SkillSet:
    """What discovery found, what it refused, and which built-ins a project replaced."""

    skills: tuple[Skill, ...]
    problems: tuple[str, ...]
    shadows: tuple[str, ...]

    def get(self, name: str) -> Skill | None:
        want = (name or "").strip().lower()
        return next((s for s in self.skills if s.name == want), None)

    def names(self) -> list[str]:
        return [s.name for s in self.skills]

    def menu(self) -> str:
        """Names and descriptions only: bodies are what ``use_skill`` is for."""
        if not self.skills:
            return ""
        return "\n".join(
            f"- {s.name}{'' if s.is_builtin else ' (project)'}: {s.description}" for s in self.skills
        )


EMPTY = SkillSet(skills=(), problems=(), shadows=())


def _shown(names: list[str]) -> str:
    """Names for a report: clipped, few and quoted, because they came from an untrusted header."""
    shown = [repr(n[:_SHOWN] + ("…" if len(n) > _SHOWN else "")) for n in names[:_SHOWN_COUNT]]
    more = len(names) - _SHOWN_COUNT
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _strip_header(text: str) -> tuple[dict[str, str], list[str], str]:
    """Split an optional ``---`` header from the body, and name the keys nothing reads.

    A small parser rather than YAML: the header holds single-line keys only, and a
    body that happens to contain a colon must not break loading.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, [], text
    header: dict[str, str] = {}
    unknown: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return header, unknown, "\n".join(lines[index + 1:])
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in HEADER_KEYS:
            header[key] = value.strip()
        elif _KEY_RE.match(key) and key not in unknown:
            unknown.append(key)
    # An unterminated header is not a header: the whole text is the body.
    return {}, [], text


def _declared_tools(value: str, codify_names: bool) -> tuple[tuple[str, ...], list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for raw in value.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if codify_names:
            name = CODIFY_MOVES.get(name, name)
        if name in kept:
            continue
        if not _TOOL_RE.match(name) or name not in KNOWN_TOOLS:
            if name not in dropped:
                dropped.append(name)
            continue
        kept.append(name)
    notes: list[str] = []
    if dropped:
        notes.append(f"names tools Arena does not have, ignored: {_shown(dropped)}")
    if len(kept) > MAX_DECLARED_TOOLS:
        notes.append(f"names {len(kept)} tools; only the first {MAX_DECLARED_TOOLS} were read")
        kept = kept[:MAX_DECLARED_TOOLS]
    return tuple(kept), notes


def _first_line(body: str) -> str:
    """The first line that could be a description: not blank, not a heading."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def parse_skill(text: str, fallback_name: str, source: str) -> tuple[Skill | None, str | None]:
    """One skill file, or the reason it is not one. Never raises."""
    if not text.strip():
        return None, f"{fallback_name}: the file is empty"
    if len(text) > MAX_SKILL_CHARS:
        return None, f"{fallback_name}: {len(text)} characters, over the {MAX_SKILL_CHARS} limit"
    header, unknown, body = _strip_header(text)
    name = (header.get("name") or fallback_name).strip().lower()
    if not NAME_RE.match(name):
        return None, (
            f"{fallback_name}: {name[:_SHOWN]!r} is not a valid skill name "
            "(lowercase letters, digits and dashes, starting with a letter or digit)"
        )
    if not body.strip():
        return None, f"{name}: the file has a header but no instructions"
    description = (header.get("description") or _first_line(body)).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    if "tools" in header:
        tools, notes = _declared_tools(header["tools"], codify_names=False)
    else:
        tools, notes = _declared_tools(header.get("moves", ""), codify_names=True)
    if unknown:
        notes.append(
            f"has header keys nothing reads: {_shown(unknown)} "
            "(a skill's header holds name, description and tools)"
        )
    return Skill(name=name, description=description, body=body.strip(), source=source,
                 tools=tools, notes=tuple(notes)), None


def render_skill(name: str, description: str, tools: Collection[str], body: str) -> str:
    """The text the Settings form writes: exactly the keys ``parse_skill`` reads."""
    lines = ["---", f"name: {name}", f"description: {description}"]
    if tools:
        lines.append(f"tools: {', '.join(tools)}")
    lines.extend(["---", "", body.strip(), ""])
    return "\n".join(lines)


def _read_file(path: Path, source: str, name: str) -> tuple[Skill | None, str | None]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SKILL_BYTES + 1)
        if len(raw) > MAX_SKILL_BYTES:
            return None, f"{name}: over {MAX_SKILL_BYTES:,} bytes, past the {MAX_SKILL_CHARS}-character limit"
        text = raw.decode("utf-8")
    except OSError as exc:
        return None, f"{path.name}: {type(exc).__name__}: {exc}"
    except UnicodeDecodeError:
        return None, f"{path.name}: not valid UTF-8 text"
    return parse_skill(text, name, source)


def _skill_files(directory: Path, label: str, within: Path | None = None) -> tuple[list[tuple[Path, str]], list[str]]:
    """The skill files in ``directory``, and a sentence for every entry ignored or refused.

    A symlink is refused: ``Path.is_file()`` follows links, so a cloned repository
    could ship ``.arena/skills/notes.md -> ~/.ssh/id_rsa`` and the file would be
    handed to the model as instructions. The directory itself must resolve to
    exactly ``<within>/<label>``, which refuses a link at either path component.
    """
    try:
        if not directory.is_dir():
            return [], []
        root = directory.resolve()
        expected = within.resolve() / label if within is not None else None
    except (OSError, RuntimeError) as exc:
        return [], [f"{label}: refused — could not resolve the skills directory ({exc})"]
    if expected is not None and root != expected:
        return [], [
            f"{label}: refused — it resolves to {root}, outside this folder's own {label}; "
            "a link to a directory elsewhere would load someone else's files as instructions"
        ]
    files: list[tuple[Path, str]] = []
    problems: list[str] = []
    told = silent = 0

    def say(sentence: str) -> None:
        nonlocal told, silent
        if told < MAX_REPORTED_PROBLEMS:
            problems.append(sentence)
            told += 1
        else:
            silent += 1

    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        return [], [f"{label}: could not be read ({exc.strerror or exc})"]
    not_examined = max(0, len(names) - MAX_SCANNED_ENTRIES)
    for path in (directory / name for name in names[:MAX_SCANNED_ENTRIES]):
        if path.is_symlink():
            say(f"{path.name}: refused — a skill file or folder must be real, inside {label}, not a link")
            continue
        if path.is_dir():
            skill_file = path / "SKILL.md"
            if skill_file.is_symlink() or not skill_file.is_file():
                say(f"{path.name}/: ignored — a skill folder must contain a real SKILL.md file")
                continue
            try:
                inside = skill_file.resolve().parent == path.resolve() and path.resolve().parent == root
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                say(f"{path.name}/SKILL.md: refused — it must be inside {label}, not a link elsewhere")
                continue
            files.append((skill_file, path.name.lower()))
            try:
                extras = sorted(
                    (e for e in itertools.islice(path.iterdir(), MAX_SCANNED_ENTRIES + 1) if e.name != "SKILL.md"),
                    key=lambda p: p.name,
                )
            except OSError:
                extras = []
            for extra in extras[:MAX_EXTRAS_LISTED]:
                say(f"{path.name}/{extra.name}: ignored — a skill is its SKILL.md; bundled files are never read or run")
            if len(extras) > MAX_EXTRAS_LISTED:
                say(f"{path.name}/: {len(extras) - MAX_EXTRAS_LISTED} more bundled files ignored")
            continue
        if path.suffix != ".md":
            say(f"{path.name}: ignored — a skill is a .md file or a folder with a SKILL.md")
            continue
        try:
            inside = path.resolve().parent == root
        except (OSError, RuntimeError):
            inside = False
        if not inside:
            say(f"{path.name}: refused — a skill file must be a real file inside {label}, not a link")
            continue
        if path.is_file():
            files.append((path, path.stem.lower()))
    if silent:
        problems.append(f"{label}: {silent} more entries were ignored or refused (not listed one by one)")
    if not_examined:
        problems.append(f"{label}: {not_examined} more entries were not examined; only the first "
                        f"{MAX_SCANNED_ENTRIES} (by name) were looked at")
    return files, problems


def _noted(skill: Skill) -> list[str]:
    return [f"{skill.source} skill {skill.name!r}: {note}" for note in skill.notes]


def _load_dir(directory: Path, label: str, source: str, within: Path | None,
              limit: int | None) -> tuple[list[Skill], list[str]]:
    files, refused = _skill_files(directory, label, within)
    kind = f"{source} skill ignored"
    problems = [f"{kind} — {p}" for p in refused]
    if limit is not None and len(files) > limit:
        problems.append(f"{label} holds {len(files)} skills; only the first {limit} were read")
        files = files[:limit]
    found: list[Skill] = []
    for path, name in files:
        skill, problem = _read_file(path, source, name)
        if skill is not None:
            found.append(skill)
            problems.extend(_noted(skill))
        elif problem:
            problems.append(f"{kind} — {problem}")
    return found, problems


def builtin_skills() -> tuple[list[Skill], list[str]]:
    return _load_dir(BUILTIN_DIR, "builtin_skills", "built-in", None, None)


def load_skills(root: str | Path | None, disabled: Collection[str] = ()) -> SkillSet:
    """The skills offered for a folder: built-ins, then .codify, then .arena, later layers winning.

    ``disabled`` is the person's Settings choice: a disabled name is not offered from
    any layer, and ``use_skill`` cannot reach it.
    """
    blocked = {str(n).strip().lower() for n in disabled}
    layers: list[tuple[list[Skill], list[str]]] = [builtin_skills()]
    if root:
        base = Path(root)
        for rel, source in WORKSPACE_LAYERS:
            layers.append(_load_dir(base / rel, rel, source, base, MAX_WORKSPACE_SKILLS))
    ordered: dict[str, Skill] = {}
    builtin_names: set[str] = set()
    shadows: list[str] = []
    problems: list[str] = []
    for found, layer_problems in layers:
        problems.extend(layer_problems)
        for skill in found:
            if skill.name in blocked:
                continue
            if skill.is_builtin:
                builtin_names.add(skill.name)
            elif skill.name in builtin_names and skill.name not in shadows:
                shadows.append(skill.name)
            ordered[skill.name] = skill
    return SkillSet(skills=tuple(ordered[n] for n in sorted(ordered)),
                    problems=tuple(problems), shadows=tuple(shadows))
