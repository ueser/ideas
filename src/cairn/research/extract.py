"""Claude passes that propose records: contextual claims (with source passages and scale
mappings) and entity identity merges. Everything they write has status "proposed"."""

from __future__ import annotations

import hashlib
import json
from typing import Callable

from ..agent import MAX_NOTE_CHARS, Claude, _parallel, _stderr
from ..config import Config
from .framework import Framework
from .model import norm_name
from .records import Records, locate, now, passage_hash, read_note_text, short_hash, study_identity

DIRECTIONS = ["increase", "decrease", "present", "absent", "unspecified"]


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def claim_schema(fw: Framework) -> dict:
    scale = {"type": "string", "enum": fw.scale_ids + ["unmapped"]}
    participant = {
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "property": {"type": "string", "description": "what about the entity: activity, expression, "
                                                          "loss-of-function variant, presence, severity, ..."},
            "direction": {"type": "string", "enum": DIRECTIONS},
            "activity_effect": {"type": "string", "enum": ["increase", "decrease", "unknown"]},
            "effect_basis": {"type": "string", "enum": ["stated", "inferred", "not_applicable"]},
            "scales": {"type": "array", "items": scale},
        },
        "required": ["entity", "property", "direction", "activity_effect", "effect_basis", "scales"],
        "additionalProperties": False,
    }
    context = {"type": "object", "properties": {f: _nullable({"type": "string"}) for f in fw.context_fields},
               "required": list(fw.context_fields), "additionalProperties": False}
    claim = {
        "type": "object",
        "properties": {
            "quote": {"type": "string"},
            "claim_type": {"type": "string", "enum": ["observation", "association", "causal", "hypothesis"]},
            "attribution": {"type": "string", "enum": ["publication", "note_author", "cited_work"]},
            "subject": participant,
            "predicate": {"type": "string", "enum": [r.id for r in fw.relations]},
            "object": participant,
            "conditions": {"type": "array", "items": participant},
            "negated": {"type": "boolean"},
            "qualifiers": {"type": "array", "items": {"type": "string"}},
            "context": context,
            "evidence": {"type": "string", "enum": [e.id for e in fw.evidence]},
            "confidence": {"type": "number"},
        },
        "required": ["quote", "claim_type", "attribution", "subject", "predicate", "object", "conditions",
                     "negated", "qualifiers", "context", "evidence", "confidence"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"study_label": {"type": "string"},
                                             "claims": {"type": "array", "items": claim}},
            "required": ["study_label", "claims"], "additionalProperties": False}


def extraction_system(fw: Framework) -> str:
    guide = f"\n\nFramework guide (for interpretation; not rules):\n{fw.guide.strip()}" if fw.guide.strip() else ""
    return f"""You turn research notes into contextual claims for a research workspace. Each claim \
interprets one passage; a reviewer will check it against that passage, so fidelity matters more than \
coverage.

{fw.describe()}{guide}

For every claim:
- quote: the shortest verbatim span of the note that supports it (copied exactly).
- subject and object are entity states: entity (a standard name, without state words), property \
(activity, expression, loss-of-function variant, presence, severity, ...), and direction as observed. \
Give both directions when the passage states them; leave "unspecified" otherwise.
- activity_effect: how this state moves the entity's own activity or level. effect_basis says whether \
the passage states it ("stated") or you are inferring it ("inferred", e.g. assuming a variant is \
inactivating); use "not_applicable" for processes and conditions.
- predicate: the relation type from the framework. claim_type: "causal" only if the passage reports \
an intervention or genetic evidence that the subject drives the object; "association" for \
co-occurrence or correlation; "observation" for a measured finding; "hypothesis" for proposals \
and speculation. Hedged claims are hypotheses or carry qualifiers.
- negated: true when the passage reports no effect / no association.
- conditions: further participants the claim depends on ("only when B is present").
- context: fill each field the passage or note states; null when unknown. Never guess context.
- attribution: whether the claim comes from the publication's results, the note author's own \
interpretation, or a work the note cites.
- scales: where each participant sits in the framework (several if it genuinely spans them, \
"unmapped" if none fits).
- confidence (0-1): how faithfully the claim represents the passage.
study_label: the citation of the study the note describes, if stated (e.g. "Lee et al. 2021").
Extract only what the note says. Return no claims when there are none."""


NORMALIZE_SYSTEM = """You resolve entity identity in a scientific knowledge base. Group names that \
refer to the same entity (abbreviations, spelling variants, Greek letters spelled out, a gene symbol \
and its protein product). Do not merge related-but-distinct entities: family members, isoforms named \
differently, a complex and one subunit, a process and its readout. Pick the most standard name as \
canonical. Omit singletons."""

NORMALIZE_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {
        "type": "object",
        "properties": {"canonical": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}}},
        "required": ["canonical", "members"], "additionalProperties": False}}},
    "required": ["groups"], "additionalProperties": False,
}


def _participant(p: dict) -> dict:
    out = {k: p[k] for k in ("entity", "property", "direction", "activity_effect", "effect_basis")}
    out["entity"] = out["entity"].strip()
    out["property"] = out["property"].strip().lower()
    return out


def build_claims(fw: Framework, note: dict, text: str, data: dict, model: str) -> tuple[list[dict], dict]:
    """Turn one extraction result into claim records plus scale-mapping votes."""
    study = study_identity(note["frontmatter"], data.get("study_label", ""), note["id"])
    claims, votes = [], {}
    for raw in data.get("claims", []):
        if not raw["subject"]["entity"].strip() or not raw["object"]["entity"].strip():
            continue
        span = locate(raw["quote"], text) or {"heading": "", "line_start": 0, "line_end": 0,
                                              "text": raw["quote"], "hash": passage_hash(raw["quote"]),
                                              "match": "not found"}
        subj, obj = _participant(raw["subject"]), _participant(raw["object"])
        cid = "c-" + short_hash(note["id"], span["hash"], norm_name(subj["entity"]), subj["property"],
                                raw["predicate"], norm_name(obj["entity"]), obj["property"], raw["negated"])
        claim = {
            "id": cid, "note": note["id"], "note_revision": note["hash"], "span": span, "study": study,
            "attribution": raw["attribution"], "claim_type": raw["claim_type"],
            "subject": subj, "predicate": raw["predicate"], "object": obj,
            "conditions": [_participant(p) for p in raw["conditions"]],
            "negated": bool(raw["negated"]), "qualifiers": raw["qualifiers"],
            "context": {f: raw["context"].get(f) for f in fw.context_fields},
            "evidence": raw["evidence"], "extraction_confidence": round(float(raw["confidence"]), 2),
            "framework": fw.ref, "status": "proposed", "status_reason": "",
            "extracted": {"by": model, "at": now()}, "supersedes": None,
        }
        claims.append(claim)
        for p_raw, p in ((raw["subject"], subj), (raw["object"], obj),
                         *zip(raw["conditions"], claim["conditions"])):
            votes.setdefault((p["entity"], p["property"]), {})[cid] = [s for s in p_raw["scales"]]
    return claims, votes


def merge_claims(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Re-extraction keeps human decisions: a reproduced claim keeps its review status; reviewed
    claims that were not reproduced are kept (sync marks them stale if their passage is gone);
    unreviewed claims that were not reproduced are dropped."""
    old = {c["id"]: c for c in existing}
    out = []
    for c in fresh:
        if c["id"] in old and old[c["id"]]["status"] != "proposed":
            keep = old[c["id"]]
            c = dict(c, status=keep["status"], status_reason=keep.get("status_reason", ""))
        out.append(c)
    fresh_ids = {c["id"] for c in fresh}
    out += [c for c in existing if c["id"] not in fresh_ids and c["status"] != "proposed"]
    return out


def apply_mapping_votes(recs: Records, fw: Framework, votes: dict, note_claim_ids: set[str]) -> None:
    maps = recs.mappings(fw.id)
    # forget this note's previous votes, then add the new ones
    for m in maps.values():
        m["sources"] = {k: v for k, v in m.get("sources", {}).items() if k not in note_claim_ids}
    for (entity, prop), srcs in votes.items():
        key = f"{entity}|{prop}"
        m = maps.setdefault(key, {"key": key, "entity": entity, "property": prop, "scales": [], "sources": {},
                                  "status": "proposed", "framework": fw.ref, "rationale": ""})
        m["sources"].update(srcs)
    for key in list(maps):
        m = maps[key]
        if not m["sources"] and m["status"] == "proposed":
            del maps[key]
            continue
        if m["status"] == "accepted":
            continue  # a reviewed mapping is not re-voted
        tally: dict[str, int] = {}
        for scales in m["sources"].values():
            for s in scales:
                tally[s] = tally.get(s, 0) + 1
        top = max(tally.values()) if tally else 0
        m["scales"] = sorted((s for s, n in tally.items() if n == top or n >= 2), key=fw.scale_index)
        m["votes"] = sum(tally.values())
        m["framework"] = fw.ref
        m["rationale"] = f"proposed by extraction from {len(m['sources'])} claim(s)"
    recs.save_mappings(fw.id, maps)


class Extractor:
    def __init__(self, cfg: Config, store, llm: Claude, fw: Framework, log: Callable[[str], None] = _stderr):
        self.cfg, self.store, self.llm, self.fw, self.log = cfg, store, llm, fw, log
        self.recs = Records(cfg)

    def _run(self, label, system, schema, prompts):
        def work(key):
            return self.llm.structured(system, prompts[key], schema)
        return _parallel(list(prompts), work, self.cfg.max_workers, label, self.log)

    def pending(self, force: bool = False, notes: list[str] | None = None) -> list[dict]:
        done = self.recs.manifest().get("extracted", {})
        out = []
        for n in self.store.notes():
            if n["id"].startswith(self.cfg.research_dir + "/"):
                continue
            if notes and n["id"] not in notes:
                continue
            prev = done.get(n["id"], {})
            if force or prev.get("revision") != n["hash"] or prev.get("framework") != self.fw.ref:
                out.append(n)
        return out

    def extract(self, force: bool = False, limit: int | None = None, notes: list[str] | None = None) -> int:
        todo = self.pending(force, notes)[: limit or None]
        known = sorted({e["canonical"] for e in self.recs.entities()} |
                       {c["subject"]["entity"] for c in self.recs.all_claims()} |
                       {c["object"]["entity"] for c in self.recs.all_claims()})
        vocab = (f"Entity names already in use (reuse them for the same entity):\n{', '.join(known[:400])}\n\n"
                 if known else "")
        prompts, texts = {}, {}
        for n in todo:
            text = read_note_text(self.cfg, n["path"]) or ""
            body, more = text, ""
            if len(body) > MAX_NOTE_CHARS:
                more = f"(Note is long; showing the first {MAX_NOTE_CHARS} of {len(body)} characters.)\n"
                body = body[:MAX_NOTE_CHARS]
            texts[n["id"]] = text
            prompts[n["id"]] = f"{vocab}Note path: {n['path']}\n{more}\n<note>\n{body}\n</note>"
        total = 0
        by_id = {n["id"]: n for n in todo}
        for nid, data in self._run("extract", extraction_system(self.fw), claim_schema(self.fw), prompts):
            with self.recs.lock():
                fresh, votes = build_claims(self.fw, by_id[nid], texts[nid], data, self.cfg.model)
                existing = self.recs.note_claims(nid)
                self.recs.save_note_claims(nid, merge_claims(existing, fresh))
                apply_mapping_votes(self.recs, self.fw, votes, {c["id"] for c in existing} | {c["id"] for c in fresh})
                m = self.recs.manifest()
                m.setdefault("extracted", {})[nid] = {"revision": by_id[nid]["hash"], "framework": self.fw.ref,
                                                      "at": now(), "claims": len(fresh)}
                self.recs.save_manifest(m)
            total += len(fresh)
        return total

    def normalize(self, force: bool = False) -> int:
        """Propose identity merges for entity names (one call per 400 names, only when names changed)."""
        names = sorted({c[r]["entity"] for c in self.recs.all_claims() for r in ("subject", "object")} |
                       {a for e in self.recs.entities() for a in e.get("aliases", [])})
        if len(names) < 2:
            return 0
        m = self.recs.manifest()
        prompts = {}
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            sig = hashlib.sha256(json.dumps(chunk).encode()).hexdigest()[:12]
            if force or sig not in m.get("normalized", []):
                prompts[sig] = "Names:\n" + "\n".join(f"- {n}" for n in chunk)
        merged = 0
        for sig, data in self._run("identity", NORMALIZE_SYSTEM, NORMALIZE_SCHEMA, prompts):
            with self.recs.lock():
                rows = {e["canonical"]: e for e in self.recs.entities()}
                for g in data["groups"]:
                    members = sorted(set(g["members"]) - {g["canonical"]})
                    if not members:
                        continue
                    e = rows.setdefault(g["canonical"], {"canonical": g["canonical"], "aliases": [],
                                                         "status": "proposed", "proposed_at": now()})
                    if e["status"] == "rejected":
                        continue
                    e["aliases"] = sorted(set(e["aliases"]) | set(members))
                    merged += len(members)
                self.recs.save_entities(list(rows.values()))
                mm = self.recs.manifest()
                mm.setdefault("normalized", []).append(sig)
                self.recs.save_manifest(mm)
        return merged
