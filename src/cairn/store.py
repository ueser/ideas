"""SQLite-backed index. Structural tables are rebuilt on every `cairn index`;
the knowledge layer written by agents (enrichment, relations, topic names)
persists across rebuilds and is keyed so it survives edits and renames."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY, path TEXT, title TEXT, tags TEXT, aliases TEXT, frontmatter TEXT,
  body TEXT, word_count INT, mtime REAL, hash TEXT, generated INT, rank REAL DEFAULT 0,
  bridge REAL DEFAULT 0, topic TEXT
);
CREATE TABLE IF NOT EXISTS links (src TEXT, dst TEXT, target TEXT, anchor TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE TABLE IF NOT EXISTS similar (src TEXT, dst TEXT, score REAL);
CREATE INDEX IF NOT EXISTS similar_src ON similar(src);
CREATE TABLE IF NOT EXISTS topics (id TEXT PRIMARY KEY, label TEXT, terms TEXT, size INT, stale INT);
CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
  note_id UNINDEXED, anchor UNINDEXED, title, heading, body, tags, tokenize='porter unicode61'
);
-- knowledge layer (persistent)
CREATE TABLE IF NOT EXISTS enrichment (note_id TEXT PRIMARY KEY, hash TEXT, data TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS relations (
  src TEXT, dst TEXT, type TEXT, reason TEXT, confidence REAL, source TEXT, created REAL,
  PRIMARY KEY (src, dst, type)
);
CREATE TABLE IF NOT EXISTS link_checked (note_id TEXT PRIMARY KEY, hash TEXT, candidates TEXT);
CREATE TABLE IF NOT EXISTS topic_meta (
  id TEXT PRIMARY KEY, name TEXT, synopsis TEXT, questions TEXT, members TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- framework lens: claims extracted per note, and entity synonym map
CREATE TABLE IF NOT EXISTS mech_claims (
  framework TEXT, note_id TEXT, subject TEXT, subject_scale TEXT, subject_change TEXT, kind TEXT,
  object TEXT, object_scale TEXT, object_change TEXT, evidence TEXT, context TEXT, quote TEXT, confidence REAL
);
CREATE INDEX IF NOT EXISTS mech_claims_fw ON mech_claims(framework, note_id);
CREATE TABLE IF NOT EXISTS mech_extracted (
  framework TEXT, note_id TEXT, hash TEXT, fw_hash TEXT, scales TEXT, PRIMARY KEY (framework, note_id)
);
CREATE TABLE IF NOT EXISTS mech_alias (framework TEXT, alias TEXT, canonical TEXT, PRIMARY KEY (framework, alias));
"""

RELATION_TYPES = [
    "supports", "contradicts", "extends", "example_of", "part_of", "prerequisite_for",
    "alternative_to", "caused_by", "same_topic",
]


def _fts_query(text: str) -> str:
    """Turn free text into a forgiving FTS5 query (OR of quoted terms, prefix on the last)."""
    words = re.findall(r"\w+", text.lower())
    if not words:
        return '""'
    terms = [f'"{w}"' for w in words]
    terms[-1] = terms[-1] + "*"
    return " OR ".join(terms)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- meta -----------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ---- notes ----------------------------------------------------------
    def note(self, note_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            row = self.db.execute("SELECT * FROM notes WHERE lower(id)=lower(?) OR lower(title)=lower(?)",
                                  (note_id, note_id)).fetchone()
        return self._note_dict(row) if row else None

    def _note_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["aliases"] = json.loads(d["aliases"] or "[]")
        d["frontmatter"] = json.loads(d["frontmatter"] or "{}")
        enr = self.enrichment(d["id"])
        d["summary"] = (enr or {}).get("summary") or d["frontmatter"].get("summary") or ""
        d["enrichment"] = enr
        return d

    def notes(self, include_generated: bool = False) -> list[dict]:
        q = "SELECT * FROM notes" + ("" if include_generated else " WHERE generated=0") + " ORDER BY id"
        return [self._note_dict(r) for r in self.db.execute(q)]

    def note_ids(self, include_generated: bool = False) -> list[str]:
        q = "SELECT id FROM notes" + ("" if include_generated else " WHERE generated=0") + " ORDER BY id"
        return [r["id"] for r in self.db.execute(q)]

    def titles(self) -> dict[str, str]:
        return {r["id"]: r["title"] for r in self.db.execute("SELECT id, title FROM notes")}

    # ---- graph queries --------------------------------------------------
    def outlinks(self, note_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT DISTINCT dst, target, anchor, kind FROM links WHERE src=? ORDER BY dst", (note_id,))]

    def backlinks(self, note_id: str) -> list[str]:
        return [r["src"] for r in self.db.execute(
            "SELECT DISTINCT l.src FROM links l JOIN notes s ON s.id=l.src "
            "WHERE l.dst=? AND l.src!=? AND s.generated=0 ORDER BY l.src", (note_id, note_id))]

    def similar(self, note_id: str, k: int = 10) -> list[tuple[str, float]]:
        return [(r["dst"], r["score"]) for r in self.db.execute(
            "SELECT dst, score FROM similar WHERE src=? ORDER BY score DESC LIMIT ?", (note_id, k))]

    def relations(self, note_id: str | None = None) -> list[dict]:
        if note_id is None:
            rows = self.db.execute("SELECT * FROM relations ORDER BY src, dst")
        else:
            rows = self.db.execute("SELECT * FROM relations WHERE src=? OR dst=? ORDER BY src, dst",
                                   (note_id, note_id))
        return [dict(r) for r in rows]

    def broken_links(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT l.src, l.target FROM links l JOIN notes n ON n.id=l.src "
            "WHERE l.dst IS NULL AND n.generated=0 ORDER BY l.src")]

    def orphans(self) -> list[str]:
        return [r["id"] for r in self.db.execute(
            "SELECT id FROM notes n WHERE generated=0 "
            "AND NOT EXISTS (SELECT 1 FROM links WHERE src=n.id AND dst IS NOT NULL AND dst!=n.id) "
            "AND NOT EXISTS (SELECT 1 FROM links l JOIN notes s ON s.id=l.src "
            "                WHERE l.dst=n.id AND l.src!=n.id AND s.generated=0) ORDER BY id")]

    # ---- topics ---------------------------------------------------------
    def topics(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM topics ORDER BY size DESC, id"):
            d = dict(r)
            d["terms"] = json.loads(d["terms"] or "[]")
            meta = self.db.execute("SELECT * FROM topic_meta WHERE id=?", (d["id"],)).fetchone()
            d["synopsis"] = meta["synopsis"] if meta else ""
            d["questions"] = json.loads(meta["questions"]) if meta and meta["questions"] else []
            out.append(d)
        return out

    def topic(self, topic_id: str) -> dict | None:
        return next((t for t in self.topics() if t["id"] == topic_id), None)

    def topic_members(self, topic_id: str) -> list[str]:
        return [r["id"] for r in self.db.execute(
            "SELECT id FROM notes WHERE topic=? ORDER BY rank DESC, id", (topic_id,))]

    # ---- search ---------------------------------------------------------
    def search(self, query: str, limit: int = 10, tag: str | None = None, topic: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT note_id, anchor, heading, bm25(sections_fts, 0, 0, 8.0, 4.0, 1.0, 3.0) AS score, "
            "snippet(sections_fts, 4, '**', '**', ' … ', 18) AS snip "
            "FROM sections_fts WHERE sections_fts MATCH ? ORDER BY score LIMIT ?",
            (_fts_query(query), limit * 8)).fetchall()
        best: dict[str, dict] = {}
        for r in rows:
            nid = r["note_id"]
            entry = best.get(nid)
            if entry is None:
                best[nid] = {"id": nid, "score": -r["score"], "section": r["heading"], "anchor": r["anchor"],
                             "snippet": r["snip"]}
            else:
                entry["score"] += -r["score"] * 0.25  # several matching sections nudge the note up
        results = []
        for nid, e in best.items():
            n = self.note(nid)
            if n is None or (tag and tag.lower() not in n["tags"]) or (topic and n["topic"] != topic):
                continue
            e.update(title=n["title"], summary=n["summary"], topic=n["topic"], generated=bool(n["generated"]))
            # generated navigation notes are useful but should not outrank real notes
            if e["generated"]:
                e["score"] *= 0.5
            results.append(e)
        results.sort(key=lambda e: -e["score"])
        return results[:limit]

    # ---- knowledge layer ------------------------------------------------
    def enrichment(self, note_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM enrichment WHERE note_id=?", (note_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def enrichment_hash(self, note_id: str) -> str | None:
        row = self.db.execute("SELECT hash FROM enrichment WHERE note_id=?", (note_id,)).fetchone()
        return row["hash"] if row else None

    def set_enrichment(self, note_id: str, note_hash: str, data: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO enrichment VALUES (?, ?, ?, ?)",
                        (note_id, note_hash, json.dumps(data), time.time()))
        self.db.commit()

    def add_relation(self, src: str, dst: str, rtype: str, reason: str = "", confidence: float = 1.0,
                     source: str = "agent") -> None:
        self.db.execute("INSERT OR REPLACE INTO relations VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (src, dst, rtype, reason, confidence, source, time.time()))
        self.db.commit()

    def clear_relations(self, src: str, source: str) -> None:
        self.db.execute("DELETE FROM relations WHERE src=? AND source=?", (src, source))
        self.db.commit()

    def link_check_state(self, note_id: str) -> tuple[str, list[str]] | None:
        row = self.db.execute("SELECT hash, candidates FROM link_checked WHERE note_id=?", (note_id,)).fetchone()
        return (row["hash"], json.loads(row["candidates"])) if row else None

    def set_link_checked(self, note_id: str, note_hash: str, candidates: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO link_checked VALUES (?, ?, ?)",
                        (note_id, note_hash, json.dumps(sorted(candidates))))
        self.db.commit()

    def set_topic_meta(self, topic_id: str, name: str, synopsis: str, questions: list[str],
                       members: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO topic_meta VALUES (?, ?, ?, ?, ?, ?)",
                        (topic_id, name, synopsis, json.dumps(questions), json.dumps(sorted(members)), time.time()))
        self.db.execute("UPDATE topics SET label=?, stale=0 WHERE id=?", (name, topic_id))
        self.db.commit()

    def topic_meta_all(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM topic_meta"):
            d = dict(r)
            d["members"] = json.loads(d["members"] or "[]")
            out.append(d)
        return out

    def rename_note_refs(self, old: str, new: str) -> None:
        """Carry the knowledge layer over when a note moves (same content hash)."""
        self.db.execute("UPDATE OR IGNORE enrichment SET note_id=? WHERE note_id=?", (new, old))
        self.db.execute("UPDATE OR IGNORE relations SET src=? WHERE src=?", (new, old))
        self.db.execute("UPDATE OR IGNORE relations SET dst=? WHERE dst=?", (new, old))
        self.db.execute("UPDATE OR IGNORE link_checked SET note_id=? WHERE note_id=?", (new, old))
        self.db.execute("UPDATE mech_claims SET note_id=? WHERE note_id=?", (new, old))
        self.db.execute("UPDATE OR IGNORE mech_extracted SET note_id=? WHERE note_id=?", (new, old))
