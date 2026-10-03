"""The navigation toolset. One implementation backs the CLI, the built-in
Claude agent, and the MCP server, so humans and any agent see the same view."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from .config import Config
from .graph import add_edge, shortest_path
from .store import RELATION_TYPES, Store
from .vault import slugify


WRITE_KINDS = ["insights", "answers", "questions", "mechanisms"]


class ToolError(Exception):
    pass


class VaultTools:
    def __init__(self, cfg: Config, store: Store, allow_writes: bool = False):
        self.cfg = cfg
        self.store = store
        self.allow_writes = allow_writes
        self.written: list[str] = []  # files created during this session
        self.framework = cfg.load_framework()
        self._mech = None
        self._mech_sig = None

    def _model(self):
        if self.framework is None:
            raise ToolError("No framework configured (set `framework = \"…toml\"` in cairn.toml).")
        from .mechanism import MechanismModel
        sig = tuple(self.store.db.execute(
            "SELECT (SELECT COUNT(*) || ':' || IFNULL(MAX(rowid), 0) FROM mech_claims WHERE framework=?), "
            "(SELECT COUNT(*) FROM mech_alias WHERE framework=?)", (self.framework.id, self.framework.id)).fetchone())
        if self._mech is None or sig != self._mech_sig:
            self._mech, self._mech_sig = MechanismModel(self.store, self.framework), sig
        return self._mech

    # ---- helpers --------------------------------------------------------
    def _resolve(self, note_id: str) -> dict:
        nid = note_id.strip().removesuffix(".md").removeprefix("[[").removesuffix("]]").split("|")[0]
        n = self.store.note(nid)
        if n is None:
            stem = nid.rsplit("/", 1)[-1].lower()
            hits = [i for i in self.store.note_ids(True) if i.rsplit("/", 1)[-1].lower() == stem]
            hits = hits or [x["id"] for x in self.store.notes() if stem in (a.lower() for a in x["aliases"])]
            if hits:
                n = self.store.note(hits[0])
        if n is None:
            sugg = [r["id"] for r in self.store.search(nid, limit=5)]
            raise ToolError(f"No note '{note_id}'." + (f" Did you mean: {', '.join(sugg)}?" if sugg else ""))
        return n

    def _brief(self, nid: str) -> dict:
        n = self.store.note(nid)
        if n is None:
            return {"id": nid}
        out = {"id": nid, "title": n["title"]}
        if n["summary"]:
            out["summary"] = n["summary"]
        return out

    def _graph(self) -> dict:
        g: dict = {}
        for r in self.store.db.execute(
                "SELECT DISTINCT l.src, l.dst FROM links l JOIN notes a ON a.id=l.src JOIN notes b ON b.id=l.dst "
                "WHERE a.generated=0 AND b.generated=0"):
            add_edge(g, r["src"], r["dst"], 1.0)
        for r in self.store.relations():
            add_edge(g, r["src"], r["dst"], 1.0)
        for r in self.store.db.execute("SELECT src, dst, score FROM similar"):
            add_edge(g, r["src"], r["dst"], r["score"])
        return g

    def _edge_reason(self, a: str, b: str) -> str:
        db = self.store.db
        if db.execute("SELECT 1 FROM links WHERE src=? AND dst=?", (a, b)).fetchone():
            return "links to"
        if db.execute("SELECT 1 FROM links WHERE src=? AND dst=?", (b, a)).fetchone():
            return "is linked from"
        rel = db.execute("SELECT type, src FROM relations WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
                         (a, b, b, a)).fetchone()
        if rel:
            return rel["type"] if rel["src"] == a else f"(inverse) {rel['type']}"
        sim = db.execute("SELECT score FROM similar WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
                         (a, b, b, a)).fetchone()
        return f"similar content ({sim['score']:.2f})" if sim else "related"

    # ---- read tools -----------------------------------------------------
    def overview(self) -> dict:
        """Map of the whole collection: size, topics, hubs, bridges, recent and loose notes."""
        s = self.store
        notes = s.notes()
        hubs = sorted(notes, key=lambda n: -n["rank"])[:8]
        bridges = [n for n in sorted(notes, key=lambda n: -n["bridge"]) if n["bridge"] > 0.3][:8]
        recent = sorted(notes, key=lambda n: -n["mtime"])[:8]
        return {
            "stats": s.get_meta("stats", {}),
            "topics": [{"id": t["id"], "name": t["label"], "size": t["size"], "synopsis": t["synopsis"],
                        "key_terms": t["terms"][:5]} for t in s.topics()],
            "hubs": [self._brief(n["id"]) for n in hubs],
            "bridges": [{"id": n["id"], "title": n["title"], "score": n["bridge"]} for n in bridges],
            "recent": [{"id": n["id"], "title": n["title"],
                        "modified": dt.datetime.fromtimestamp(n["mtime"]).strftime("%Y-%m-%d")} for n in recent],
            "unclustered": [n["id"] for n in notes if not n["topic"]][:30],
            "tags": dict(Counter(t for n in notes for t in n["tags"]).most_common(25)),
        }

    def search(self, query: str, limit: int = 10, tag: str | None = None, topic: str | None = None) -> list[dict]:
        """Full-text search (BM25 over sections). Returns best-matching notes with a snippet."""
        return self.store.search(query, limit=limit, tag=tag, topic=topic)

    def read_note(self, note_id: str, section: str | None = None, max_chars: int = 12000) -> dict:
        """Full note content plus metadata. `section` limits output to one heading."""
        n = self._resolve(note_id)
        body = n["body"]
        if section:
            rows = self.store.db.execute(
                "SELECT heading, body FROM sections_fts WHERE note_id=? AND (lower(heading)=lower(?) OR anchor=?)",
                (n["id"], section, slugify(section))).fetchall()
            if not rows:
                heads = [r["heading"] for r in self.store.db.execute(
                    "SELECT heading FROM sections_fts WHERE note_id=?", (n["id"],)) if r["heading"]]
                raise ToolError(f"No section '{section}' in {n['id']}. Sections: {heads}")
            body = "\n\n".join(f"## {r['heading']}\n{r['body']}" for r in rows)
        truncated = len(body) > max_chars
        out = {
            "id": n["id"], "title": n["title"], "path": n["path"], "tags": n["tags"], "topic": n["topic"],
            "modified": dt.datetime.fromtimestamp(n["mtime"]).strftime("%Y-%m-%d"),
            "summary": n["summary"], "content": body[:max_chars],
        }
        if n["enrichment"]:
            e = n["enrichment"]
            out["key_claims"] = e.get("key_claims", [])
            out["open_questions"] = e.get("open_questions", [])
        if truncated:
            out["truncated"] = f"showing {max_chars} of {len(body)} chars; read by section to see more"
        return out

    def note_links(self, note_id: str) -> dict:
        """Everything connected to a note: outgoing/back links, typed relations, similar notes."""
        n = self._resolve(note_id)
        nid = n["id"]
        out_links, broken = [], []
        for link in self.store.outlinks(nid):
            if link["dst"] is None:
                broken.append(link["target"])
            elif link["dst"] != nid:
                out_links.append(self._brief(link["dst"]))
        rels = []
        for r in self.store.relations(nid):
            other = r["dst"] if r["src"] == nid else r["src"]
            direction = "outgoing" if r["src"] == nid else "incoming"
            rels.append({"type": r["type"], "direction": direction, "note": self._brief(other), "reason": r["reason"]})
        linked = {x["id"] for x in out_links} | set(self.store.backlinks(nid))
        similar = [{**self._brief(d), "score": s, "already_linked": d in linked}
                   for d, s in self.store.similar(nid, 8)]
        return {"id": nid, "title": n["title"], "topic": n["topic"],
                "outgoing": out_links, "backlinks": [self._brief(b) for b in self.store.backlinks(nid)],
                "relations": rels, "similar": similar, "broken_links": broken}

    def topic(self, topic_id: str) -> dict:
        """A topic (cluster of related notes): synopsis, members, and links to neighbouring topics."""
        t = self.store.topic(topic_id)
        if t is None:
            match = [x for x in self.store.topics() if x["label"].lower() == topic_id.lower()]
            if not match:
                raise ToolError(f"No topic '{topic_id}'. Topics: {[x['id'] for x in self.store.topics()]}")
            t = match[0]
        members = self.store.topic_members(t["id"])
        member_set = set(members)
        neighbours: Counter = Counter()
        g = self._graph()
        for m in members:
            for u, w in g.get(m, {}).items():
                if u not in member_set:
                    other = self.store.note(u)
                    if other and other["topic"]:
                        neighbours[other["topic"]] += w
        names = {x["id"]: x["label"] for x in self.store.topics()}
        return {"id": t["id"], "name": t["label"], "synopsis": t["synopsis"], "key_terms": t["terms"],
                "open_questions": t["questions"], "notes": [self._brief(m) for m in members],
                "neighbouring_topics": [{"id": k, "name": names.get(k, k), "strength": round(v, 2)}
                                        for k, v in neighbours.most_common(5)]}

    def find_path(self, from_note: str, to_note: str) -> dict:
        """Shortest chain of connections between two notes, with the reason for each hop."""
        a, b = self._resolve(from_note)["id"], self._resolve(to_note)["id"]
        path = shortest_path(self._graph(), a, b)
        if not path:
            return {"from": a, "to": b, "path": None, "note": "no connection within 6 hops"}
        hops = [{"from": x, "to": y, "via": self._edge_reason(x, y)} for x, y in zip(path, path[1:])]
        return {"from": a, "to": b, "path": [self._brief(p) for p in path], "hops": hops}

    def list_notes(self, tag: str | None = None, topic: str | None = None, sort: str = "rank",
                   limit: int = 50) -> list[dict]:
        """List notes, optionally filtered by tag or topic, sorted by rank (centrality), recent, or title."""
        notes = self.store.notes()
        if tag:
            notes = [n for n in notes if tag.lower().lstrip("#") in n["tags"]]
        if topic:
            notes = [n for n in notes if n["topic"] == topic]
        key = {"rank": lambda n: -n["rank"], "recent": lambda n: -n["mtime"],
               "title": lambda n: n["title"].lower()}.get(sort, lambda n: -n["rank"])
        return [{"id": n["id"], "title": n["title"], "topic": n["topic"], "tags": n["tags"],
                 "summary": n["summary"]} for n in sorted(notes, key=key)[:limit]]

    def maintenance_report(self) -> dict:
        """Collection health: broken links, orphans, unsummarised notes, likely duplicates."""
        dupes = [dict(r) for r in self.store.db.execute(
            "SELECT src, dst, score FROM similar WHERE score >= 0.6 AND src < dst ORDER BY score DESC LIMIT 20")]
        notes = self.store.notes()
        return {
            "broken_links": self.store.broken_links(),
            "orphans": self.store.orphans(),
            "not_enriched": [n["id"] for n in notes if self.store.enrichment_hash(n["id"]) != n["hash"]],
            "possible_duplicates": dupes,
            "contradictions": [r for r in self.store.relations() if r["type"] == "contradicts"],
            "stale_topics": [t["id"] for t in self.store.topics() if t["stale"]],
        }

    # ---- framework lens tools ------------------------------------------
    def mechanism_overview(self) -> dict:
        """The framework lens: scales, how many entities each holds, loop consistency, natural chain targets."""
        return self._model().overview()

    def mechanism_entity(self, name: str) -> dict:
        """One entity in the multi-scale model: its scale, upstream causes, downstream effects, associations
        (each with sign, confidence, quotes and source notes) and the incoherent loops it sits in."""
        rep = self._model().entity_report(name)
        if rep is None:
            sugg = [e for e in self._model().entities if name.lower() in e.lower()][:8]
            raise ToolError(f"No entity '{name}'." + (f" Similar: {sugg}" if sugg else ""))
        return rep

    def mechanism_chains(self, target: str, source: str | None = None, k: int = 5) -> list[dict]:
        """Best chains of claims climbing the framework's scales and ending at `target` (optionally starting
        at `source`), with per-step evidence, missing scales and open points to test."""
        m = self._model()
        if m.find_entity(target) is None:
            raise ToolError(f"No entity '{target}'. Likely targets: {m.default_targets()}")
        return m.chains(target, source=source, k=k)

    def mechanism_consistency(self, entity: str | None = None) -> dict:
        """Triangulation: loops whose signs multiply to '-' (incoherent), links with conflicting claims,
        and the best-triangulated links. Optionally restricted to loops through one entity."""
        return self._model().consistency(entity)

    # ---- write tools (agent knowledge layer; never touch human prose) ---
    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise ToolError("Write tools are disabled for this session.")

    def add_relation(self, src: str, dst: str, type: str, reason: str) -> dict:
        """Record a typed, explained relation between two notes in the knowledge layer."""
        self._require_writes()
        if type not in RELATION_TYPES:
            raise ToolError(f"type must be one of {RELATION_TYPES}")
        a, b = self._resolve(src)["id"], self._resolve(dst)["id"]
        self.store.add_relation(a, b, type, reason, 1.0, "agent")
        return {"ok": True, "relation": f"{a} --{type}--> {b}"}

    def write_note(self, kind: str, title: str, body: str, sources: list[str]) -> dict:
        """Create a new note (insight / answer / question) inside the generated folder, citing sources."""
        self._require_writes()
        if kind not in WRITE_KINDS:
            raise ToolError(f"kind must be one of {WRITE_KINDS}")
        cited = [self._resolve(s)["id"] for s in sources]
        if not cited:
            raise ToolError("cite at least one source note")
        folder = self.cfg.out_path / kind
        folder.mkdir(parents=True, exist_ok=True)
        date = dt.date.today().isoformat()
        path = folder / f"{date}-{slugify(title)[:60]}.md"
        n = 2
        while path.exists():
            path = folder / f"{date}-{slugify(title)[:60]}-{n}.md"
            n += 1
        from .render import link as mk_link
        fm = ["---", f"title: \"{title.replace(chr(34), chr(39))}\"", f"type: {kind[:-1]}",
              f"created: {date}", "generated_by: cairn", "sources:"]
        fm += [f"  - \"{c}\"" for c in cited]
        fm.append("---")
        refs = "\n".join(f"- {mk_link(self.cfg, c, self.store.titles().get(c, c), path)}" for c in cited)
        text = "\n".join(fm) + f"\n\n# {title}\n\n{body.strip()}\n\n## Sources\n\n{refs}\n"
        path.write_text(text, encoding="utf-8")
        rel = path.relative_to(self.cfg.root).as_posix()
        self.written.append(rel)
        return {"ok": True, "path": rel}


# Tool specs shared by the agent and the MCP server --------------------------

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}
_I = {"type": "integer"}

# (name, group, input schema); group is "read", "mech" (needs a framework) or "write"
TOOL_SPECS = [
    ("overview", "read", _schema({}, [])),
    ("search", "read", _schema({"query": _S, "limit": _I, "tag": _S, "topic": _S}, ["query"])),
    ("read_note", "read", _schema({"note_id": _S, "section": _S}, ["note_id"])),
    ("note_links", "read", _schema({"note_id": _S}, ["note_id"])),
    ("topic", "read", _schema({"topic_id": _S}, ["topic_id"])),
    ("find_path", "read", _schema({"from_note": _S, "to_note": _S}, ["from_note", "to_note"])),
    ("list_notes", "read", _schema({"tag": _S, "topic": _S,
                                    "sort": {"type": "string", "enum": ["rank", "recent", "title"]}, "limit": _I}, [])),
    ("maintenance_report", "read", _schema({}, [])),
    ("mechanism_overview", "mech", _schema({}, [])),
    ("mechanism_entity", "mech", _schema({"name": _S}, ["name"])),
    ("mechanism_chains", "mech", _schema({"target": _S, "source": _S, "k": _I}, ["target"])),
    ("mechanism_consistency", "mech", _schema({"entity": _S}, [])),
    ("add_relation", "write", _schema({"src": _S, "dst": _S, "type": {"type": "string", "enum": RELATION_TYPES},
                                       "reason": _S}, ["src", "dst", "type", "reason"])),
    ("write_note", "write", _schema({"kind": {"type": "string", "enum": WRITE_KINDS},
                                     "title": _S, "body": _S, "sources": {"type": "array", "items": _S}},
                                    ["kind", "title", "body", "sources"])),
]


def tool_description(name: str) -> str:
    doc = getattr(VaultTools, name).__doc__ or name
    return re.sub(r"\s+", " ", doc).strip()


def available_tools(tools: VaultTools) -> list[str]:
    groups = {"read"} | ({"write"} if tools.allow_writes else set()) | ({"mech"} if tools.framework else set())
    return [name for name, group, _ in TOOL_SPECS if group in groups]


def tool_definitions(tools: VaultTools) -> list[dict]:
    names = set(available_tools(tools))
    return [{"name": name, "description": tool_description(name), "input_schema": schema}
            for name, _, schema in TOOL_SPECS if name in names]


def call_tool(tools: VaultTools, name: str, args: dict):
    if name not in available_tools(tools):
        raise ToolError(f"unknown tool {name}")
    return getattr(tools, name)(**args)

