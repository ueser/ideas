"""cairn command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

from .config import load_config
from .indexer import build_index
from .render import Renderer
from .store import RELATION_TYPES, Store
from .tools import ToolError, VaultTools
from .vault import iter_markdown

DEFAULT_TOML = """# cairn configuration (all keys optional)
output_dir = "_cairn"        # generated navigation notes live here
link_style = "wiki"          # "wiki" ([[note|Title]]) or "markdown" ([Title](note.md))
write_note_blocks = false    # append a managed Connections block to every note
exclude = []                 # extra glob patterns to ignore, e.g. ["templates/*"]
model = "claude-opus-5-5"
effort = "medium"            # bulk passes: enrich / link / topic naming
agent_effort = "high"        # ask / insights agents
max_workers = 4
"""


def _open(args):
    cfg = load_config(args.vault)
    store = Store(cfg.db_path)
    return cfg, store


def _fresh(cfg, store, quiet: bool = True) -> None:
    """Re-index when any note changed since the last index (cheap mtime scan)."""
    last = store.get_meta("indexed_at", 0)
    count = store.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    paths = list(iter_markdown(cfg.root, cfg.exclude))
    if last and len(paths) == count and all(p.stat().st_mtime <= last for p in paths):
        return
    stats = build_index(cfg, store)
    if not quiet:
        _err(f"indexed {stats['notes']} notes in {stats['seconds']}s")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print(obj, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        human(obj)


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent)


def _llm(cfg, store):
    from .agent import Claude, Organizer
    return Organizer(cfg, store, Claude(cfg))


# ---- commands ---------------------------------------------------------------

def cmd_init(args):
    cfg = load_config(args.vault)
    toml = cfg.root / "cairn.toml"
    if not toml.exists():
        toml.write_text(DEFAULT_TOML)
        print(f"wrote {toml}")
    gi = cfg.root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if ".cairn/" not in lines:
        gi.write_text("\n".join(lines + [".cairn/"]) + "\n")
        print("added .cairn/ to .gitignore")
    cmd_organize(args)


def cmd_index(args):
    cfg, store = _open(args)
    stats = build_index(cfg, store)
    _print(stats, args.json, lambda s: print(", ".join(f"{k}: {v}" for k, v in s.items())))


def cmd_organize(args):
    cfg, store = _open(args)
    if getattr(args, "note_blocks", False):
        cfg.write_note_blocks = True
    build_index(cfg, store)
    changed = Renderer(cfg, store).render_all()
    build_index(cfg, store)  # pick up regenerated navigation notes for search
    print(f"organized {cfg.root}: {len(changed)} file(s) updated; start at {cfg.output_dir}/INDEX.md")
    for c in changed[:40]:
        print(f"  {c}")


def cmd_overview(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    ov = VaultTools(cfg, store).overview()

    def human(o):
        st = o["stats"]
        print(f"{st.get('notes', 0)} notes, {st.get('topics', 0)} topics, {st.get('links', 0)} links, "
              f"{st.get('relations', 0)} relations, {st.get('enriched', 0)} enriched\n")
        print("Topics:")
        for t in o["topics"]:
            print(f"  {t['id']:<32} {t['name']} ({t['size']})")
            if t["synopsis"]:
                print(_wrap(t["synopsis"], "      "))
        print("\nHubs:      " + ", ".join(h["id"] for h in o["hubs"]))
        if o["bridges"]:
            print("Bridges:   " + ", ".join(b["id"] for b in o["bridges"]))
        print("Recent:    " + ", ".join(r["id"] for r in o["recent"]))
        if o["unclustered"]:
            print("Loose:     " + ", ".join(o["unclustered"]))
    _print(ov, args.json, human)


def cmd_search(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    res = VaultTools(cfg, store).search(" ".join(args.query), limit=args.limit, tag=args.tag, topic=args.topic)

    def human(rs):
        if not rs:
            print("no matches")
        for r in rs:
            sec = f" › {r['section']}" if r["section"] else ""
            print(f"{r['id']}{sec}  ({r['score']:.1f})")
            if r["summary"]:
                print(_wrap(r["summary"]))
            print(_wrap(r["snippet"].replace("\n", " ")))
    _print(res, args.json, human)


def cmd_show(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    n = VaultTools(cfg, store).read_note(args.note, section=args.section, max_chars=args.max_chars)

    def human(n):
        print(f"# {n['title']}  [{n['id']}]  topic={n['topic']}  tags={','.join(n['tags'])}")
        if n["summary"]:
            print(f"> {n['summary']}")
        print()
        print(n["content"])
    _print(n, args.json, human)


def cmd_links(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    info = VaultTools(cfg, store).note_links(args.note)

    def human(i):
        print(f"{i['title']} [{i['id']}]  topic={i['topic']}")
        for label, key in (("→ links to", "outgoing"), ("← linked from", "backlinks")):
            if i[key]:
                print(f"  {label}: " + ", ".join(x["id"] for x in i[key]))
        for r in i["relations"]:
            arrow = f"--{r['type']}-->" if r["direction"] == "outgoing" else f"<--{r['type']}--"
            print(f"  {arrow} {r['note']['id']}" + (f"  ({r['reason']})" if r["reason"] else ""))
        if i["similar"]:
            print("  ≈ similar: " + ", ".join(f"{s['id']} {s['score']:.2f}{'' if s['already_linked'] else '*'}"
                                             for s in i["similar"]) +
                  ("   (* = not linked yet)" if any(not s["already_linked"] for s in i["similar"]) else ""))
        if i["broken_links"]:
            print("  ✗ broken: " + ", ".join(i["broken_links"]))
    _print(info, args.json, human)


def cmd_topic(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    tools = VaultTools(cfg, store)
    if not args.topic:
        res = [{"id": t["id"], "name": t["label"], "size": t["size"]} for t in store.topics()]
        _print(res, args.json, lambda rs: [print(f"{t['id']:<32} {t['name']} ({t['size']})") for t in rs])
        return
    t = tools.topic(args.topic)

    def human(t):
        print(f"{t['name']} [{t['id']}]")
        if t["synopsis"]:
            print(_wrap(t["synopsis"], "  "))
        print("  terms: " + ", ".join(t["key_terms"]))
        for n in t["notes"]:
            print(f"  - {n['id']}" + (f": {n['summary']}" if n.get("summary") else ""))
        if t["neighbouring_topics"]:
            print("  neighbours: " + ", ".join(x["id"] for x in t["neighbouring_topics"]))
    _print(t, args.json, human)


def cmd_path(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    res = VaultTools(cfg, store).find_path(args.a, args.b)

    def human(r):
        if not r["path"]:
            print(r["note"])
            return
        print(r["hops"][0]["from"] if r["hops"] else r["from"])
        for h in r["hops"]:
            print(f"  └─ {h['via']} → {h['to']}")
    _print(res, args.json, human)


def cmd_notes(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    res = VaultTools(cfg, store).list_notes(tag=args.tag, topic=args.topic, sort=args.sort, limit=args.limit)
    _print(res, args.json, lambda rs: [print(f"{n['id']:<40} {n['title']}") for n in rs])


def cmd_doctor(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    rep = VaultTools(cfg, store).maintenance_report()

    def human(r):
        for key, items in r.items():
            print(f"{key.replace('_', ' ')}: {len(items)}")
            for it in items[:15]:
                print(f"  - {it if isinstance(it, str) else json.dumps(it, ensure_ascii=False)}")
    _print(rep, args.json, human)


def cmd_relate(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    res = VaultTools(cfg, store, allow_writes=True).add_relation(args.src, args.dst, args.type, args.reason)
    print(res["relation"])


def cmd_enrich(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    n = _llm(cfg, store).enrich(force=args.force, limit=args.limit)
    print(f"enriched {n} note(s)")


def cmd_link(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    n = _llm(cfg, store).link(force=args.force, limit=args.limit)
    print(f"recorded {n} relation(s)")


def cmd_run(args):
    """Full pipeline: index → enrich → link → name topics → render."""
    cfg, store = _open(args)
    build_index(cfg, store)
    if not args.no_llm:
        org = _llm(cfg, store)
        _err(f"enriched {org.enrich(limit=args.limit)} note(s)")
        build_index(cfg, store)  # enrichment sharpens similarity
        _err(f"recorded {org.link(limit=args.limit)} relation(s)")
        build_index(cfg, store)  # relations reshape topics
        _err(f"named {org.name_topics()} topic(s)")
    if args.note_blocks:
        cfg.write_note_blocks = True
    changed = Renderer(cfg, store).render_all()
    stats = build_index(cfg, store)
    print(f"{stats['notes']} notes · {stats['topics']} topics · {stats['relations']} relations · "
          f"{len(changed)} file(s) updated → {cfg.output_dir}/INDEX.md")


def _progress(msg: str) -> None:
    _err(f"  · {msg[:160]}")


def cmd_ask(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    answer, written = _llm(cfg, store).ask(" ".join(args.question), save=args.save,
                                           on_event=None if args.quiet else _progress)
    print(answer)
    if written:
        Renderer(cfg, store).render_all()
        _err(f"saved: {', '.join(written)}")


def cmd_insights(args):
    cfg, store = _open(args)
    _fresh(cfg, store)
    summary, written = _llm(cfg, store).insights(n=args.n, focus=args.focus,
                                                 on_event=None if args.quiet else _progress)
    print(summary)
    build_index(cfg, store)
    Renderer(cfg, store).render_all()
    for w in written:
        _err(f"wrote {w}")


def cmd_mcp(args):
    from .mcp_server import serve
    cfg, store = _open(args)
    _fresh(cfg, store)
    serve(cfg, store, allow_writes=args.allow_writes)


# ---- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cairn", description="Organise a folder of markdown notes for humans and agents.")
    p.add_argument("--vault", "-C", default=os.environ.get("CAIRN_VAULT", "."), help="notes folder (default: .)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "create cairn.toml, ignore .cairn/, build the first index").add_argument(
        "--note-blocks", action="store_true")
    add("index", cmd_index, "(re)build the index")
    add("organize", cmd_organize, "rebuild index and navigation notes (no LLM)").add_argument(
        "--note-blocks", action="store_true", help="also write a Connections block into each note")
    add("overview", cmd_overview, "map of the collection")
    sp = add("search", cmd_search, "full-text search")
    sp.add_argument("query", nargs="+")
    sp.add_argument("-n", "--limit", type=int, default=10)
    sp.add_argument("--tag")
    sp.add_argument("--topic")
    sp = add("show", cmd_show, "print a note (or one section)")
    sp.add_argument("note")
    sp.add_argument("--section")
    sp.add_argument("--max-chars", type=int, default=50_000)
    add("links", cmd_links, "a note's links, backlinks, relations and similar notes").add_argument("note")
    add("topic", cmd_topic, "list topics, or show one").add_argument("topic", nargs="?")
    sp = add("path", cmd_path, "how two notes connect")
    sp.add_argument("a")
    sp.add_argument("b")
    sp = add("notes", cmd_notes, "list notes")
    sp.add_argument("--tag")
    sp.add_argument("--topic")
    sp.add_argument("--sort", choices=["rank", "recent", "title"], default="rank")
    sp.add_argument("-n", "--limit", type=int, default=50)
    add("doctor", cmd_doctor, "broken links, orphans, duplicates, contradictions")
    sp = add("relate", cmd_relate, "record a typed relation between two notes")
    sp.add_argument("src")
    sp.add_argument("type", choices=RELATION_TYPES)
    sp.add_argument("dst")
    sp.add_argument("reason")
    for name, fn, help_ in (("enrich", cmd_enrich, "LLM: summarise and tag changed notes"),
                            ("link", cmd_link, "LLM: discover typed relations between similar notes")):
        sp = add(name, fn, help_)
        sp.add_argument("--force", action="store_true")
        sp.add_argument("--limit", type=int)
    sp = add("run", cmd_run, "full pipeline: index, enrich, link, name topics, render")
    sp.add_argument("--no-llm", action="store_true")
    sp.add_argument("--note-blocks", action="store_true")
    sp.add_argument("--limit", type=int, help="cap notes processed per LLM pass")
    sp = add("ask", cmd_ask, "LLM agent: answer a question from the notes, with citations")
    sp.add_argument("question", nargs="+")
    sp.add_argument("--save", action="store_true", help="save the answer under <output_dir>/answers/")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp = add("insights", cmd_insights, "LLM agent: explore across topics and write insight notes")
    sp.add_argument("-n", type=int, default=3)
    sp.add_argument("--focus")
    sp.add_argument("-q", "--quiet", action="store_true")
    add("mcp", cmd_mcp, "serve the navigation tools over MCP (stdio)").add_argument(
        "--allow-writes", action="store_true", help="expose add_relation and write_note")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except ToolError as e:
        _err(f"cairn: {e}")
        return 1
    except Exception as e:  # surface LLM/config errors without a traceback
        from .agent import LLMError
        if isinstance(e, LLMError):
            _err(f"cairn: {e}")
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
