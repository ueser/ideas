"""Build the index: parse notes, resolve links, compute similarity, rank,
communities (topics) and bridges. Deterministic and offline."""

from __future__ import annotations

import json
import time

from .config import Config
from .graph import add_edge, bridge_scores, louvain, pagerank
from .similarity import TfIdf
from .store import Store
from .vault import Resolver, load_vault, slugify


NAV_TYPES = {"index", "topic", "maintenance", "guide", "mechanism", "mechanism-scale", "chains", "consistency"}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def build_index(cfg: Config, store: Store) -> dict:
    t0 = time.time()
    notes = load_vault(cfg.root, cfg.exclude, cfg.output_dir)
    db = store.db

    # carry the knowledge layer across renames (same content, new path)
    previous = {r["id"]: r["hash"] for r in db.execute("SELECT id, hash FROM notes")}
    current_ids = {n.id for n in notes}
    gone_by_hash = {h: i for i, h in previous.items() if i not in current_ids}
    for n in notes:
        if n.id not in previous and n.hash in gone_by_hash:
            store.rename_note_refs(gone_by_hash.pop(n.hash), n.id)

    resolver = Resolver(notes)
    db.execute("DELETE FROM notes")
    db.execute("DELETE FROM links")
    db.execute("DELETE FROM similar")
    db.execute("DELETE FROM sections_fts")
    db.execute("DELETE FROM topics")

    for n in notes:
        db.execute(
            "INSERT INTO notes (id, path, title, tags, aliases, frontmatter, body, word_count, mtime, hash, generated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (n.id, n.path.relative_to(cfg.root).as_posix(), n.title, json.dumps(n.tags), json.dumps(n.aliases),
             json.dumps(n.frontmatter, default=str), n.body, n.word_count, n.mtime, n.hash, int(n.generated)))
        for link in n.links:
            db.execute("INSERT INTO links VALUES (?,?,?,?,?)",
                       (n.id, resolver.resolve(link.target, n.id), link.target, link.anchor, link.kind))
        if n.generated and n.frontmatter.get("type") in NAV_TYPES:
            continue  # navigation pages are for browsing; keep them out of search results
        tag_text = " ".join(n.tags)
        for s in n.sections:
            db.execute("INSERT INTO sections_fts VALUES (?,?,?,?,?,?)",
                       (n.id, s.anchor, n.title, s.heading, s.text, tag_text))

    # drop knowledge about notes that no longer exist
    for table, cols in (("enrichment", ["note_id"]), ("link_checked", ["note_id"]), ("relations", ["src", "dst"]),
                        ("mech_claims", ["note_id"]), ("mech_extracted", ["note_id"])):
        for col in cols:
            db.execute(f"DELETE FROM {table} WHERE {col} NOT IN (SELECT id FROM notes)")
    db.commit()

    real = [n for n in notes if not n.generated]
    real_ids = [n.id for n in real]
    real_set = set(real_ids)

    # lexical similarity, boosted by agent enrichment when present
    docs = {}
    for n in real:
        enr = store.enrichment(n.id) or {}
        extra = " ".join([enr.get("summary", "")] + enr.get("topics", []) + enr.get("entities", []))
        docs[n.id] = f"{n.title} {n.title} {' '.join(n.tags)} {' '.join(n.aliases)} {extra} {n.body}"
    tfidf = TfIdf(docs)
    neighbours = tfidf.neighbors(cfg.related_k, cfg.min_similarity)
    for src, lst in neighbours.items():
        for dst, score in lst:
            db.execute("INSERT INTO similar VALUES (?,?,?)", (src, dst, round(score, 4)))

    # graph: explicit links + agent relations + similarity
    g: dict = {v: {} for v in real_ids}
    directed = []
    for r in db.execute("SELECT DISTINCT src, dst FROM links WHERE dst IS NOT NULL"):
        if r["src"] in real_set and r["dst"] in real_set and r["src"] != r["dst"]:
            add_edge(g, r["src"], r["dst"], 1.0)
            directed.append((r["src"], r["dst"]))
    for rel in store.relations():
        if rel["src"] in real_set and rel["dst"] in real_set:
            add_edge(g, rel["src"], rel["dst"], 0.8 * (rel["confidence"] or 1.0))
            directed.append((rel["src"], rel["dst"]))
    for src, lst in neighbours.items():
        for dst, score in lst:
            add_edge(g, src, dst, score)  # added from both sides, so mutual neighbours weigh more

    ranks = pagerank(real_ids, directed)
    comm = louvain(g, real_ids)
    bridges = bridge_scores(g, comm)

    groups: dict[int, list[str]] = {}
    for nid, c in comm.items():
        groups.setdefault(c, []).append(nid)

    # stable topic ids: reuse a previous topic when membership overlaps enough
    metas = store.topic_meta_all()
    used: set[str] = set()
    topic_of: dict[str, str] = {}
    for c, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(members) < 2:
            continue
        mset = set(members)
        best, best_j = None, 0.0
        for m in metas:
            j = _jaccard(mset, set(m["members"]))
            if j > best_j and m["id"] not in used:
                best, best_j = m, j
        terms = tfidf.top_terms(members, 6)
        if best and best_j >= 0.4:
            tid, label, stale = best["id"], best["name"], int(best_j < 0.8)
        else:
            base = "t-" + slugify("-".join(terms[:2]) or f"topic-{c}")
            tid, n = base, 2
            while tid in used or any(m["id"] == tid for m in metas):
                tid, n = f"{base}-{n}", n + 1
            label, stale = " / ".join(terms[:3]).title(), 1
        used.add(tid)
        db.execute("INSERT INTO topics VALUES (?,?,?,?,?)", (tid, label, json.dumps(terms), len(members), stale))
        for nid in members:
            topic_of[nid] = tid

    for nid in real_ids:
        db.execute("UPDATE notes SET rank=?, bridge=?, topic=? WHERE id=?",
                   (round(ranks.get(nid, 0.0) * len(real_ids), 4), round(bridges.get(nid, 0.0), 4),
                    topic_of.get(nid), nid))
    db.commit()

    stats = {
        "notes": len(real),
        "generated_notes": len(notes) - len(real),
        "links": db.execute("SELECT COUNT(*) FROM links l JOIN notes n ON n.id=l.src "
                            "WHERE l.dst IS NOT NULL AND n.generated=0").fetchone()[0],
        "broken_links": len(store.broken_links()),
        "topics": len(used),
        "orphans": len(store.orphans()),
        "relations": len(store.relations()),
        "enriched": db.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0],
        "seconds": round(time.time() - t0, 2),
    }
    store.set_meta("indexed_at", time.time())
    store.set_meta("stats", stats)
    return stats
