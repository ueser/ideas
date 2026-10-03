"""Expose the navigation toolset over MCP (stdio) so any MCP client — Claude Code,
Claude Desktop, or your own agent — can browse the notes the same way."""

from __future__ import annotations

import functools

from .config import Config
from .store import Store
from .tools import VaultTools, available_tools, tool_description

INSTRUCTIONS = """Tools for navigating a collection of markdown notes organised by cairn.
Start with `overview` for the map of topics, then `topic`, `search`, `read_note` and `note_links`.
Use `find_path` to see how two ideas connect. If mechanism_* tools are present, a framework lens is
configured: they expose claims organised by scale, chains up the scales, and loop consistency.
Cite notes by id when you report findings."""


def build_server(cfg: Config, store: Store, allow_writes: bool = False):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("cairn mcp needs the MCP SDK: pip install 'cairn-notes[mcp]'") from e

    from .cli import _fresh  # local import: cli imports this module lazily too

    tools = VaultTools(cfg, store, allow_writes=allow_writes)
    server = FastMCP("cairn", instructions=INSTRUCTIONS)

    for name in available_tools(tools):
        method = getattr(tools, name)

        @functools.wraps(method)
        def handler(*args, __method=method, **kwargs):
            _fresh(cfg, store)  # notes may have changed since the server started
            return __method(*args, **kwargs)

        server.add_tool(handler, name=name, description=tool_description(name))

    @server.resource("cairn://index", name="index", description="The generated INDEX.md", mime_type="text/markdown")
    def index_resource() -> str:
        path = cfg.out_path / "INDEX.md"
        return path.read_text(encoding="utf-8") if path.exists() else "Run `cairn organize` to generate the index."

    return server


def serve(cfg: Config, store: Store, allow_writes: bool = False) -> None:
    build_server(cfg, store, allow_writes).run("stdio")
