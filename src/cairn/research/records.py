"""Canonical research records, stored as files next to the notes.

  _research/records/claims/<note-id>.jsonl   one claim per line, anchored to a passage
  _research/records/mappings/<framework>.jsonl  entity-state -> scale mappings
  _research/records/entities.jsonl           canonical entities and aliases
  _research/records/reviews.jsonl            append-only review decisions
  _research/manifest.json                    source identities and revisions
  _research/cases/*.md, narratives/**.md, questions/*.md   reasoning records (Markdown + frontmatter)

All writes go through `Records` (atomic replace under a lock). SQLite and the in-memory
model are rebuildable projections of these files.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from ..config import Config
from ..vault import HEADING_RE, MANAGED_RE, parse_frontmatter, slugify

LIVE = ("proposed", "accepted", "deferred")  # statuses an analysis may build on
DEAD = ("rejected", "superseded", "stale", "source_missing")
NOTES_MARK = "<!-- reviewer-notes: text below this line is kept when the record is regenerated -->"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def passage_hash(text: str) -> str:
    return hashlib.sha256(norm_ws(text).lower().encode()).hexdigest()[:16]


def short_hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:10]


def read_note_text(cfg: Config, note_path: str) -> str | None:
    p = cfg.root / note_path
    if not p.exists():
        return None
    # drop cairn's managed block; it sits at the end, so earlier line numbers are unaffected
    return MANAGED_RE.sub("", p.read_text(encoding="utf-8", errors="replace"))


def _heading_before(lines: list[str], idx: int) -> str:
    for i in range(idx, -1, -1):
        m = HEADING_RE.match(lines[i])
        if m:
            return m.group(2).strip()
    return ""


def locate(quote: str, text: str) -> dict | None:
    """Find a passage in the note. Exact (whitespace-insensitive) first, then a fuzzy match over
    windows of 1-3 lines. Returns a span with line numbers (1-based), heading and hash."""
    q = norm_ws(quote)
    if not q:
        return None
    lines = text.splitlines()
    # map normalized text offsets back to lines
    flat, owner = [], []
    for i, line in enumerate(lines):
        piece = norm_ws(line)
        if piece:
            if flat:
                flat.append(" ")
                owner.append(i)
            flat.append(piece)
            owner.extend([i] * len(piece))
    joined = "".join(flat)
    pos = joined.lower().find(q.lower())
    if pos >= 0:
        start, end = owner[pos], owner[min(pos + len(q) - 1, len(owner) - 1)]
        return {"heading": _heading_before(lines, start), "line_start": start + 1, "line_end": end + 1,
                "text": joined[pos:pos + len(q)], "hash": passage_hash(q), "match": "exact"}
    best, best_ratio = None, 0.0
    for i in range(len(lines)):
        for w in (1, 2, 3):
            window = norm_ws(" ".join(lines[i:i + w]))
            if not window:
                continue
            r = difflib.SequenceMatcher(None, q.lower(), window.lower()).ratio()
            if r > best_ratio:
                best, best_ratio = (i, i + w - 1, window), r
    if best and best_ratio >= 0.85:
        i, j, window = best
        return {"heading": _heading_before(lines, i), "line_start": i + 1, "line_end": j + 1, "text": window,
                "hash": passage_hash(window), "match": f"fuzzy {best_ratio:.2f}"}
    return None


def relocate(span: dict, text: str) -> dict | None:
    """Find the same passage (by its text) in a new revision of the note."""
    found = locate(span["text"], text)
    if found and found["match"] == "exact":
        found["match"] = span.get("match", "exact")
        return found
    return None


def study_identity(frontmatter: dict, label: str, note_id: str) -> dict:
    """Prefer a DOI/PMID from the note's frontmatter, then a normalised citation label.
    Without either the note is its own study, which is flagged as unknown provenance."""
    for key in ("doi", "pmid", "pmcid", "study_id"):
        if frontmatter.get(key):
            return {"id": f"{key}:{str(frontmatter[key]).strip().lower()}", "label": label or str(frontmatter[key]),
                    "basis": key}
    label = (label or str(frontmatter.get("source") or frontmatter.get("citation") or "")).strip()
    if label:
        core = re.sub(r"\(.*?\)|\bet al\.?|[^\w\s]", " ", label.lower())
        return {"id": "study:" + slugify(core), "label": label, "basis": "citation"}
    return {"id": f"note:{note_id}", "label": note_id, "basis": "unknown"}


def _reviewer() -> str:
    name = os.environ.get("CAIRN_REVIEWER") or os.environ.get("USER") or os.environ.get("USERNAME")
    if not name:
        try:
            import getpass
            name = getpass.getuser()
        except Exception:  # no passwd entry in some containers
            name = "unknown"
    return name


def dump_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}: [{', '.join(json.dumps(x) for x in v)}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {json.dumps(str(v))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


class Records:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.research_path
        self.rec = self.root / "records"
        self._claims: dict[str, dict] | None = None

    # ---- io helpers -------------------------------------------------------
    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        if not rows:
            path.unlink(missing_ok=True)
            return
        self._atomic_write(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))

    # ---- claims -----------------------------------------------------------
    def claims_path(self, note_id: str) -> Path:
        return self.rec / "claims" / f"{note_id}.jsonl"

    def all_claims(self) -> list[dict]:
        if self._claims is None:
            self._claims = {}
            base = self.rec / "claims"
            if base.is_dir():
                for p in sorted(base.rglob("*.jsonl")):
                    for c in self._read_jsonl(p):
                        self._claims[c["id"]] = c
        return list(self._claims.values())

    def claim(self, claim_id: str) -> dict | None:
        self.all_claims()
        return self._claims.get(claim_id)

    def note_claims(self, note_id: str) -> list[dict]:
        return self._read_jsonl(self.claims_path(note_id))

    def save_note_claims(self, note_id: str, claims: list[dict]) -> None:
        self._write_jsonl(self.claims_path(note_id), sorted(claims, key=lambda c: (c["span"]["line_start"], c["id"])))
        self._claims = None

    def update_claims(self, changed: list[dict]) -> None:
        by_note: dict[str, list[dict]] = {}
        for c in changed:
            by_note.setdefault(c["note"], []).append(c)
        for note_id, items in by_note.items():
            rows = {c["id"]: c for c in self.note_claims(note_id)}
            rows.update({c["id"]: c for c in items})
            self.save_note_claims(note_id, list(rows.values()))

    # ---- mappings, entities, reviews, manifest ----------------------------
    def mappings(self, framework_id: str) -> dict[str, dict]:
        return {m["key"]: m for m in self._read_jsonl(self.rec / "mappings" / f"{framework_id}.jsonl")}

    def save_mappings(self, framework_id: str, maps: dict[str, dict]) -> None:
        self._write_jsonl(self.rec / "mappings" / f"{framework_id}.jsonl", sorted(maps.values(), key=lambda m: m["key"]))

    def entities(self) -> list[dict]:
        return self._read_jsonl(self.rec / "entities.jsonl")

    def save_entities(self, rows: list[dict]) -> None:
        self._write_jsonl(self.rec / "entities.jsonl", sorted(rows, key=lambda e: e["canonical"].lower()))

    def alias_map(self) -> dict[str, str]:
        from .model import norm_name
        out = {}
        for e in self.entities():
            if e.get("status") in ("rejected",):
                continue
            for a in e.get("aliases", []) + [e["canonical"]]:
                out[norm_name(a)] = e["canonical"]
        return out

    def reviews(self) -> list[dict]:
        return self._read_jsonl(self.rec / "reviews.jsonl")

    def add_review(self, target: str, action: str, rationale: str = "", revision: str = "") -> dict:
        row = {"at": now(), "target": target, "action": action, "rationale": rationale,
               "target_revision": revision, "reviewer": _reviewer()}
        path = self.rec / "reviews.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def manifest(self) -> dict:
        p = self.root / "manifest.json"
        return json.loads(p.read_text()) if p.exists() else {"notes": {}, "extracted": {}}

    def save_manifest(self, m: dict) -> None:
        m["updated"] = now()
        self._atomic_write(self.root / "manifest.json", json.dumps(m, indent=1, sort_keys=True) + "\n")

    # ---- markdown records (cases, narratives, questions) -------------------
    def docs(self, kind: str) -> list[dict]:
        base = self.root / kind
        out = []
        if base.is_dir():
            for p in sorted(base.rglob("*.md")):
                text = p.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                if fm.get("id"):
                    out.append({"path": p, "fm": fm, "body": body})
        return out

    def doc(self, kind: str, doc_id: str) -> dict | None:
        return next((d for d in self.docs(kind) if d["fm"]["id"] == doc_id), None)

    def write_doc(self, path: Path, fm: dict, body: str) -> str:
        """Write a reasoning record. A human review status and reviewer notes survive regeneration.
        Returns 'created', 'updated' or 'unchanged'."""
        notes = ""
        if path.exists():
            old = path.read_text(encoding="utf-8")
            old_fm, _ = parse_frontmatter(old)
            if NOTES_MARK in old:
                notes = old.split(NOTES_MARK, 1)[1].strip("\n")
            old_status = old_fm.get("status", "proposed")
            if old_status != "proposed":  # keep the human decision
                fm["status"] = old_status
                for key in ("status_reason", "reviewed_at"):
                    if key in old_fm:
                        fm[key] = old_fm[key]
                # a reviewed analysis whose inputs changed goes back to review
                if old_status in ("accepted", "deferred") and old_fm.get("inputs") != fm.get("inputs"):
                    fm["status"], fm["status_reason"] = "stale", "inputs changed since review"
        text = dump_frontmatter(fm) + "\n" + body.rstrip() + f"\n\n{NOTES_MARK}\n" + (notes + "\n" if notes else "")
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return "unchanged"
        existed = path.exists()
        self._atomic_write(path, text)
        return "updated" if existed else "created"

    def set_doc_status(self, doc: dict, status: str, reason: str = "") -> None:
        text = doc["path"].read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        fm["status"] = status
        fm["reviewed_at"] = now()
        if reason:
            fm["status_reason"] = reason
        notes = body.split(NOTES_MARK, 1)
        body_main = notes[0].rstrip()
        tail = notes[1].strip("\n") if len(notes) > 1 else ""
        self._atomic_write(doc["path"], dump_frontmatter(fm) + "\n" + body_main.lstrip("\n") +
                           f"\n\n{NOTES_MARK}\n" + (tail + "\n" if tail else ""))


def sync(cfg: Config, notes) -> dict:
    """Bring records in line with the current notes: follow renames, re-anchor passages that
    still exist, mark interpretations stale when their passage changed, and mark cases or
    narratives stale when a claim they depend on is no longer live."""
    recs = Records(cfg)
    report = {"renamed": [], "changed": [], "removed": [], "stale_claims": [], "stale_docs": []}
    with recs.lock():
        m = recs.manifest()
        prev = m.get("notes", {})
        current = {n.id: n for n in notes if not n.generated}
        gone = {i: rec for i, rec in prev.items() if i not in current}
        by_rev = {rec["revision"]: i for i, rec in gone.items()}
        for nid, n in current.items():
            if nid not in prev and n.hash in by_rev:
                old = by_rev.pop(n.hash)
                moved = [dict(c, note=nid) for c in recs.note_claims(old)]
                if moved:
                    recs.save_note_claims(nid, moved)
                    recs.claims_path(old).unlink(missing_ok=True)
                if old in m.get("extracted", {}):
                    m["extracted"][nid] = m["extracted"].pop(old)
                gone.pop(old, None)
                report["renamed"].append([old, nid])
        for nid, n in current.items():
            claims = recs.note_claims(nid)
            if not claims:
                continue
            text = read_note_text(cfg, n.path.relative_to(cfg.root).as_posix()) or ""
            changed = False
            for c in claims:
                if c["note_revision"] == n.hash:
                    continue
                found = relocate(c["span"], text)
                if found:
                    c["span"].update({k: found[k] for k in ("heading", "line_start", "line_end")})
                    c["note_revision"] = n.hash
                elif c["status"] not in DEAD:
                    c["status"], c["status_reason"] = "stale", f"passage changed or removed (note revision {n.hash})"
                    report["stale_claims"].append(c["id"])
                changed = True
            if changed:
                recs.save_note_claims(nid, claims)
                report["changed"].append(nid)
        for nid in gone:
            claims = recs.note_claims(nid)
            for c in claims:
                if c["status"] not in ("rejected", "superseded"):
                    c["status"], c["status_reason"] = "source_missing", "source note was removed"
                    report["stale_claims"].append(c["id"])
            if claims:
                recs.save_note_claims(nid, claims)
            report["removed"].append(nid)
        m["notes"] = {i: {"path": n.path.relative_to(cfg.root).as_posix(), "revision": n.hash}
                      for i, n in current.items()}
        recs.save_manifest(m)

        report["stale_docs"] = mark_stale_docs(recs)
    return report


def mark_stale_docs(recs: Records) -> list[str]:
    """Cases and narratives built on claims that are no longer live go back to review."""
    live = {c["id"] for c in recs.all_claims() if c["status"] in LIVE}
    out = []
    for kind in ("cases", "narratives"):
        for d in recs.docs(kind):
            if d["fm"].get("status") in ("rejected", "stale"):
                continue
            dead = [i for i in d["fm"].get("inputs", []) if i not in live]
            if dead:
                recs.set_doc_status(d, "stale", f"input claims no longer live: {', '.join(dead[:5])}")
                out.append(d["fm"]["id"])
    return out
