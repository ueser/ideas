"""Render the navigation layer as plain markdown inside the vault:

  <output_dir>/INDEX.md         entry point: topics, hubs, bridges, recent, insights
  <output_dir>/AGENTS.md        how an agent should navigate this collection
  <output_dir>/topics/*.md      one map-of-content per topic cluster
  <output_dir>/maintenance.md   broken links, orphans, duplicates, contradictions

and, when enabled, a managed "Connections" block at the end of each note.
Human-written text is never modified outside the managed block markers.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from .config import Config
from .store import Store
from .tools import VaultTools
from .vault import MANAGED_END, MANAGED_RE, MANAGED_START

GENERATED_MARK = "generated_by: cairn"


def link(cfg: Config, target_id: str, title: str, from_path: Path | None = None) -> str:
    title = title.replace("|", "-").replace("]", ")").replace("[", "(")
    if cfg.link_style == "markdown":
        target = cfg.root / f"{target_id}.md"
        base = from_path.parent if from_path else cfg.root
        rel = os.path.relpath(target, base).replace(os.sep, "/").replace(" ", "%20")
        return f"[{title}]({rel})"
    return f"[[{target_id}|{title}]]" if title != target_id.rsplit("/", 1)[-1] else f"[[{target_id}]]"


def _write_if_changed(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def _header(title: str, kind: str) -> str:
    return f"---\ntitle: \"{title}\"\ntype: {kind}\n{GENERATED_MARK}\n---\n\n# {title}\n"


def _one_line(text: str, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class Renderer:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.tools = VaultTools(cfg, store)
        self.titles = store.titles()
        self.changed: list[str] = []

    def L(self, nid: str, at: Path) -> str:
        return link(self.cfg, nid, self.titles.get(nid, nid), at)

    def topic_id_path(self, tid: str) -> str:
        return f"{self.cfg.output_dir}/topics/{tid}"

    def _put(self, path: Path, text: str) -> None:
        if _write_if_changed(path, text):
            self.changed.append(path.relative_to(self.cfg.root).as_posix())

    # ---------------------------------------------------------------------
    def render_all(self) -> list[str]:
        out = self.cfg.out_path
        topics = self.store.topics()
        for t in topics:
            self.render_topic(t)
        self._cleanup_topics({t["id"] for t in topics})
        self.titles = self.store.titles()  # topic pages were (re)created
        self.render_index(topics)
        self.render_maintenance()
        self._put(out / "AGENTS.md", self.agents_guide())
        if self.cfg.write_note_blocks:
            for n in self.store.notes():
                self.render_note_block(n)
        return self.changed

    def render_topic(self, t: dict) -> None:
        path = self.cfg.out_path / "topics" / f"{t['id']}.md"
        info = self.tools.topic(t["id"])
        lines = [_header(t["label"], "topic")]
        if t["synopsis"]:
            lines.append(t["synopsis"].strip() + "\n")
        lines.append(f"*Key terms:* {', '.join(t['terms'][:6])}\n")
        lines.append("## Notes\n")
        for m in info["notes"]:
            s = f" — {_one_line(m['summary'])}" if m.get("summary") else ""
            lines.append(f"- {self.L(m['id'], path)}{s}")
        rels = [r for r in self.store.relations() if r["src"] in {m["id"] for m in info["notes"]}]
        if rels:
            lines.append("\n## Key relations\n")
            for r in rels[:25]:
                why = f" — {_one_line(r['reason'], 120)}" if r["reason"] else ""
                lines.append(f"- {self.L(r['src'], path)} **{r['type'].replace('_', ' ')}** "
                             f"{self.L(r['dst'], path)}{why}")
        if info["open_questions"]:
            lines.append("\n## Open questions\n")
            lines += [f"- {q}" for q in info["open_questions"]]
        if info["neighbouring_topics"]:
            lines.append("\n## Neighbouring topics\n")
            for nt in info["neighbouring_topics"]:
                lines.append(f"- {link(self.cfg, self.topic_id_path(nt['id']), nt['name'], path)}")
        lines.append(f"\n← {link(self.cfg, f'{self.cfg.output_dir}/INDEX', 'Index', path)}")
        self._put(path, "\n".join(lines) + "\n")

    def _cleanup_topics(self, keep: set[str]) -> None:
        folder = self.cfg.out_path / "topics"
        if not folder.is_dir():
            return
        for f in folder.glob("*.md"):
            if f.stem not in keep and GENERATED_MARK in f.read_text(encoding="utf-8")[:300]:
                f.unlink()
                self.changed.append(f.relative_to(self.cfg.root).as_posix() + " (removed)")

    def render_index(self, topics: list[dict]) -> None:
        path = self.cfg.out_path / "INDEX.md"
        ov = self.tools.overview()
        st = ov["stats"]
        lines = [_header("Index", "index")]
        lines.append(f"{st.get('notes', 0)} notes · {len(topics)} topics · {st.get('links', 0)} links · "
                     f"{st.get('relations', 0)} typed relations · updated {dt.date.today().isoformat()}\n")
        lines.append(f"Agents: read {link(self.cfg, f'{self.cfg.output_dir}/AGENTS', 'AGENTS', path)} first.\n")
        lines.append("## Topics\n")
        for t in topics:
            members = self.store.topic_members(t["id"])[:4]
            lead = ", ".join(self.L(m, path) for m in members)
            syn = f" — {_one_line(t['synopsis'], 140)}" if t["synopsis"] else ""
            lines.append(f"- **{link(self.cfg, self.topic_id_path(t['id']), t['label'], path)}** "
                         f"({t['size']}){syn}  \n  {lead}")
        lines.append("\n## Hubs (most central notes)\n")
        lines += [f"- {self.L(h['id'], path)}" for h in ov["hubs"]]
        if ov["bridges"]:
            lines.append("\n## Bridges (connect separate topics)\n")
            lines += [f"- {self.L(b['id'], path)}" for b in ov["bridges"]]
        lines.append("\n## Recently changed\n")
        lines += [f"- {self.L(r['id'], path)} · {r['modified']}" for r in ov["recent"]]
        generated = [n for n in self.store.notes(include_generated=True)
                     if n["generated"] and re.match(rf"{re.escape(self.cfg.output_dir)}/(insights|answers|questions)/",
                                                    n["id"])]
        if generated:
            lines.append("\n## Insights & answers\n")
            for n in sorted(generated, key=lambda n: n["id"], reverse=True)[:20]:
                lines.append(f"- {self.L(n['id'], path)}")
        if ov["unclustered"]:
            lines.append("\n## Unclustered notes\n")
            lines += [f"- {self.L(u, path)}" for u in ov["unclustered"]]
        lines.append(f"\nSee also: {link(self.cfg, f'{self.cfg.output_dir}/maintenance', 'Maintenance', path)}")
        self._put(path, "\n".join(lines) + "\n")

    def render_maintenance(self) -> None:
        path = self.cfg.out_path / "maintenance.md"
        rep = self.tools.maintenance_report()
        lines = [_header("Maintenance", "maintenance")]
        sections = [
            ("Broken links", [f"- {self.L(b['src'], path)} → `{b['target']}`" for b in rep["broken_links"]]),
            ("Orphans (no links in or out)", [f"- {self.L(o, path)}" for o in rep["orphans"]]),
            ("Possible duplicates", [f"- {self.L(d['src'], path)} ≈ {self.L(d['dst'], path)} ({d['score']:.2f})"
                                     for d in rep["possible_duplicates"]]),
            ("Contradictions to resolve", [f"- {self.L(c['src'], path)} vs {self.L(c['dst'], path)} — "
                                           f"{_one_line(c['reason'], 140)}" for c in rep["contradictions"]]),
            ("Not yet enriched (run `cairn enrich`)", [f"- {self.L(n, path)}" for n in rep["not_enriched"][:50]]),
        ]
        for title, items in sections:
            lines.append(f"## {title}\n")
            lines += items or ["- none"]
            lines.append("")
        self._put(path, "\n".join(lines))

    def render_note_block(self, n: dict) -> None:
        path = self.cfg.root / n["path"]
        info = self.tools.note_links(n["id"])
        parts = []
        if n["topic"]:
            t = self.store.topic(n["topic"])
            parts.append(f"Topic: {link(self.cfg, self.topic_id_path(n['topic']), t['label'] if t else n['topic'], path)}")
        if info["backlinks"]:
            parts.append("Backlinks: " + ", ".join(self.L(b["id"], path) for b in info["backlinks"][:12]))
        rel_lines = []
        for r in info["relations"]:
            if r["direction"] == "outgoing":
                rel_lines.append(f"  - *{r['type'].replace('_', ' ')}* {self.L(r['note']['id'], path)}")
            else:
                rel_lines.append(f"  - {self.L(r['note']['id'], path)} *{r['type'].replace('_', ' ')}* this")
        if rel_lines:
            parts.append("Relations:\n" + "\n".join(rel_lines[:12]))
        related = [s for s in info["similar"] if not s["already_linked"]][:5]
        if related:
            parts.append("Related: " + ", ".join(self.L(s["id"], path) for s in related))
        raw = path.read_text(encoding="utf-8")
        stripped = MANAGED_RE.sub("", raw).rstrip()
        if not parts:
            new = stripped + "\n"
        else:
            block = "\n".join(f"- {p}" for p in parts)
            new = f"{stripped}\n\n{MANAGED_START}\n---\n**Connections** *(maintained by cairn)*\n\n{block}\n{MANAGED_END}\n"
        if new.rstrip() != raw.rstrip():
            path.write_text(new, encoding="utf-8")
            self.changed.append(n["path"])

    def agents_guide(self) -> str:
        od = self.cfg.output_dir
        return _header("Guide for agents", "guide") + f"""\nThis folder is a collection of markdown notes organised by **cairn**.
Navigation files are regenerated; human notes are the source of truth.

## Layout

- `{od}/INDEX.md` — start here: topics, hub notes, bridges, recent changes.
- `{od}/topics/<topic>.md` — a map of one cluster of related notes, with summaries,
  typed relations (supports / contradicts / extends / …) and open questions.
- `{od}/maintenance.md` — broken links, orphans, duplicates, contradictions.
- `{od}/insights/`, `{od}/answers/` — derived notes written by agents. Each cites
  its sources; treat them as secondary to the notes they cite.
- Everything else is a human note. Links use `[[path/to/note|Title]]` (wiki style)
  or relative markdown links.

## How to find things

1. Read `INDEX.md`, pick the relevant topic page, then open individual notes.
2. For keyword lookups use `cairn search "<query>"` (BM25 over sections) — far
   cheaper than reading files one by one.
3. For a note's neighbourhood use `cairn links <note>`: outgoing links, backlinks,
   typed relations and similar notes it doesn't link to yet.
4. To see how two ideas connect use `cairn path <a> <b>`.
5. If the `cairn` MCP server is configured, the same operations are available as
   tools: overview, search, read_note, note_links, topic, find_path, list_notes.

## Rules when you write

- Never edit text between `{MANAGED_START}` and `{MANAGED_END}`; it is regenerated.
- Do not hand-edit files in `{od}/topics/`, `INDEX.md` or `maintenance.md`.
- Ground every conclusion in specific notes and cite them with links.
- Prefer recording a typed relation (`cairn relate`) over rewriting a human note.
- After adding or editing notes, run `cairn organize` to refresh this layer.
"""

