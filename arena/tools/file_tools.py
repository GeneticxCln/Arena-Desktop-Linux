"""
File operation tools: read_file, write_file, edit_file, search, and ls.
"""

import os
import re
from pathlib import Path
from typing import Dict, Any

from arena.tools.base import BaseTool


def _resolve_workspace_path(cwd: str, raw_path: str, *, write: bool = False) -> Path:
    """Resolve a path inside the active workspace root, blocking traversal and sensitive git metadata."""
    root = Path(cwd).expanduser().resolve()
    p = Path(raw_path).expanduser()
    target = (p if p.is_absolute() else root / p).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"Path '{raw_path}' escapes workspace '{root}'")
    if write:
        try:
            parts = target.relative_to(root).parts
        except ValueError:
            parts = ()
        if len(parts) >= 2 and parts[0] == ".git" and parts[1] in ("config", "hooks"):
            raise PermissionError(f"Refusing to modify protected git metadata: '{raw_path}'")
    return target


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read a file from disk with line numbers."

    def execute(self, params: Dict[str, Any]) -> str:
        raw_path = params.get("path", "").strip()
        if not raw_path:
            return "Error: No file path provided."

        try:
            f = _resolve_workspace_path(self.cwd, raw_path, write=False)
        except PermissionError as e:
            return f"Error: {e}"

        if not f.exists():
            return f"Error: File not found at '{raw_path}'"
        if not f.is_file():
            return f"Error: '{raw_path}' is a directory, not a file."

        try:
            size = f.stat().st_size
            if size > 500_000:
                return f"Error: File is too large ({size} bytes). Max readable size is 500KB."

            text = f.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()

            if len(lines) > 300:
                head = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines[:150]))
                tail = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines[-50:], len(lines) - 50))
                omitted = len(lines) - 200
                return f"File '{f.name}' ({len(lines)} lines):\n{head}\n  ... [{omitted} lines omitted] ...\n{tail}"

            return f"File '{f.name}' ({len(lines)} lines):\n" + "\n".join(
                f"{i+1:4d} | {line}" for i, line in enumerate(lines)
            )
        except Exception as e:
            return f"Error reading file '{raw_path}': {e}"


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Create or overwrite a file on disk."

    def execute(self, params: Dict[str, Any]) -> str:
        raw_path = params.get("path", "").strip()
        content = params.get("content", "")

        if not raw_path:
            return "Error: No file path provided."

        try:
            f = _resolve_workspace_path(self.cwd, raw_path, write=True)
        except PermissionError as e:
            return f"Error: {e}"

        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
            line_count = len(content.splitlines())
            return f"Successfully wrote '{f.name}' ({line_count} lines, {len(content)} bytes)."
        except Exception as e:
            return f"Error writing file '{raw_path}': {e}"


class EditFileTool(BaseTool):
    name = "edit_file"
    description = "Targeted find-and-replace in an existing file."

    def execute(self, params: Dict[str, Any]) -> str:
        raw_path = params.get("path", "").strip()
        old_text = params.get("old_text", "")
        new_text = params.get("new_text", "")

        if not raw_path:
            return "Error: No file path provided."
        if not old_text:
            return "Error: 'old_text' cannot be empty."

        try:
            f = _resolve_workspace_path(self.cwd, raw_path, write=True)
        except PermissionError as e:
            return f"Error: {e}"

        if not f.exists():
            return f"Error: File not found at '{raw_path}'"

        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if old_text not in content:
                return (
                    f"Error: Target text not found in '{f.name}'. "
                    f"Please make sure the string matches existing file contents exactly."
                )

            # Replace first occurrence
            updated = content.replace(old_text, new_text, 1)
            f.write_text(updated, encoding="utf-8")
            return f"Successfully edited '{f.name}'."
        except Exception as e:
            return f"Error editing file '{raw_path}': {e}"


class SearchTool(BaseTool):
    name = "search"
    description = "Search for regex patterns across files."

    def execute(self, params: Dict[str, Any]) -> str:
        pattern = params.get("pattern", "")
        root_dir = params.get("path", ".")

        if not pattern:
            return "Error: No regex pattern provided."

        try:
            target = _resolve_workspace_path(self.cwd, root_dir, write=False)
        except PermissionError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"Error: Path '{root_dir}' does not exist."

        ws_root = Path(self.cwd).expanduser().resolve()
        results = []
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".tox", ".mypy_cache"}
        binary_extensions = {".exe", ".dll", ".bin", ".zip", ".gz", ".png", ".jpg", ".pyc", ".iso"}

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Error: Invalid regex pattern '{pattern}': {e}"

        def _iter_files():
            if target.is_file():
                yield target
                return
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
                for fn in sorted(filenames):
                    yield Path(dirpath) / fn

        scanned = 0
        for f in _iter_files():
            if scanned >= 600:
                break
            if f.is_symlink():
                try:
                    resolved = f.resolve()
                    if resolved != ws_root and ws_root not in resolved.parents:
                        continue
                except OSError:
                    continue
            if not f.is_file() or f.suffix.lower() in binary_extensions:
                continue
            scanned += 1
            try:
                if f.stat().st_size > 500_000:
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        try:
                            rel = f.relative_to(ws_root)
                        except ValueError:
                            rel = f
                        results.append(f"{rel}:{i}: {line.strip()[:140]}")
                        if len(results) >= 40:
                            return "\n".join(results) + "\n... [Max 40 matches returned]"
            except Exception:
                continue

        return "\n".join(results) if results else "No matches found."


class LsTool(BaseTool):
    name = "ls"
    description = "List files and directories with sizes."

    def execute(self, params: Dict[str, Any]) -> str:
        raw_path = params.get("path", ".")
        try:
            target = _resolve_workspace_path(self.cwd, raw_path, write=False)
        except PermissionError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"Error: Path '{raw_path}' does not exist."

        if target.is_file():
            sz = target.stat().st_size
            return f"{target.name} ({sz} bytes)"

        try:
            items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            output_lines = []
            for item in items[:100]:
                if item.name.startswith(".") and item.name not in (".env",):
                    continue
                if item.is_dir():
                    output_lines.append(f"  📁 {item.name}/")
                else:
                    try:
                        sz = item.stat().st_size
                    except OSError:
                        output_lines.append(f"  🔗 {item.name:<35s} (broken symlink)")
                        continue
                    human_sz = (
                        f"{sz/1048576:.1f}MB" if sz > 1048576
                        else f"{sz/1024:.1f}KB" if sz > 1024
                        else f"{sz}B"
                    )
                    output_lines.append(f"  📄 {item.name:<35s} {human_sz}")

            return "\n".join(output_lines) if output_lines else "(directory is empty)"
        except Exception as e:
            return f"Error listing directory '{raw_path}': {e}"

