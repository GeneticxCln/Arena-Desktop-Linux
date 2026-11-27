"""
Markdown and tool execution output rendering.
Supports code blocks, sandbox deployment previews, and search citations.
"""

from typing import Dict, Any, List
from rich.markup import escape as _escape_markup
from rich.panel import Panel
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.text import Text

from arena.console import con


def render_markdown(text: str) -> None:
    """Render markdown text with syntax highlighted code blocks."""
    md = Markdown(text)
    con.print(md)


def print_tool_call(name: str, params: Dict[str, Any]) -> None:
    """Print an active tool call indicator."""
    param_str = ", ".join(
        f"[dim]{_escape_markup(str(k))}=[/dim]{_escape_markup(repr(v)[:45])}"
        for k, v in params.items()
    )
    con.print(
        f"\n  [bold #299BFF]⚡ Tool Execution:[/bold #299BFF] "
        f"[bold #F4F0EB]{_escape_markup(str(name))}[/bold #F4F0EB]({param_str})"
    )


def print_tool_result(name: str, result: str) -> None:
    """Print formatted result of tool execution."""
    text = str(result or "")
    preview = text[:400] + ("\n... [truncated]" if len(text) > 400 else "")
    panel = Panel(
        Text(preview),
        title=f"[dim]Output: {_escape_markup(str(name))}[/dim]",
        border_style="dim",
        width=80,
    )
    con.print(panel)


def print_deployment_preview(url: str) -> None:
    """Print a prominent announcement box when code is built & deployed live."""
    safe_url = _escape_markup(str(url or ""))
    panel = Panel(
        f"[bold #10B981]🚀 Live Application Preview Deployed on Arena:[/bold #10B981]\n"
        f"[bold cyan]{safe_url}[/bold cyan]\n"
        f"[dim]Click URL or open in browser to preview interactive build[/dim]",
        border_style="#10B981",
        title="[bold #10B981] Web Sandbox Deployment [/bold #10B981]",
        width=80,
    )
    con.print(panel)


def print_search_sources(sources: List[Dict[str, Any]]) -> None:
    """Render search grounding citations and URLs."""
    if not sources:
        return
    con.print("\n[bold #06B6D4]🌐 Sources & Citations:[/bold #06B6D4]")
    for i, s in enumerate(sources, 1):
        if isinstance(s, dict):
            title = _escape_markup(str(s.get("title") or s.get("name") or "Web Source"))
            url = _escape_markup(str(s.get("url") or s.get("link") or ""))
            con.print(f"  [{i}] [bold white]{title}[/bold white] - [dim]{url}[/dim]")
        elif isinstance(s, str):
            con.print(f"  [{i}] [dim]{_escape_markup(s)}[/dim]")
