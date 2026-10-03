import asyncio
import json

from cairn.cli import main
from cairn.config import load_config
from cairn.mcp_server import build_server


def test_cli_init_and_queries(vault, capsys):
    assert main(["-C", str(vault), "init"]) == 0
    assert (vault / "cairn.toml").exists() and ".cairn/" in (vault / ".gitignore").read_text()
    assert (vault / "_cairn" / "INDEX.md").exists()
    capsys.readouterr()
    assert main(["-C", str(vault), "search", "deep", "work", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res[0]["id"] == "work/deep-work"
    assert main(["-C", str(vault), "relate", "work/multitasking", "contradicts", "deep-work", "switching costs"]) == 0
    capsys.readouterr()
    assert main(["-C", str(vault), "--json", "links", "multitasking"]) == 0
    assert json.loads(capsys.readouterr().out)["relations"][0]["type"] == "contradicts"
    assert main(["-C", str(vault), "show", "no-such-note-xyz"]) == 1


def test_cli_auto_reindexes_on_change(vault, capsys):
    main(["-C", str(vault), "index"])
    (vault / "new-note.md").write_text("# Zettelkasten\n\nAtomic notes with unique ids.\n")
    capsys.readouterr()
    main(["-C", str(vault), "--json", "search", "zettelkasten"])
    assert json.loads(capsys.readouterr().out)[0]["id"] == "new-note"


def test_config_file(vault):
    (vault / "cairn.toml").write_text('output_dir = "/nav/"\nlink_style = "markdown"\nunknown = 1\n')
    cfg = load_config(vault)
    assert cfg.output_dir == "nav" and cfg.link_style == "markdown"


def test_mcp_server_lists_and_calls_tools(indexed):
    cfg, store = indexed
    server = build_server(cfg, store)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"overview", "search", "read_note", "note_links", "topic", "find_path"} <= names
    assert "write_note" not in names
    result = asyncio.run(server.call_tool("search", {"query": "caffeine"}))
    assert "sleep/caffeine" in json.dumps(result, default=str)
    writes = {t.name for t in asyncio.run(build_server(cfg, store, allow_writes=True).list_tools())}
    assert "write_note" in writes
