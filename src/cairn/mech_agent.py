"""Claude passes for a framework lens: claim extraction, entity normalisation,
and the narrative agent that turns ranked chains into a cited mechanism story
with testable hypotheses."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Callable

from .agent import MAX_NOTE_CHARS, Claude, _parallel, _stderr
from .config import Config
from .framework import Framework
from .mechanism import MechanismModel, norm_name, store_claims
from .store import Store
from .tools import VaultTools


def claim_schema(fw: Framework) -> dict:
    scale = {"type": "string", "enum": fw.scale_ids}
    change = {"type": "string", "enum": ["increase", "decrease", "unclear"]}
    return {
        "type": "object",
        "properties": {
            "note_scales": {"type": "array", "items": scale, "description": "scales this note talks about"},
            "claims": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"}, "subject_scale": scale, "subject_change": change,
                    "kind": {"type": "string", "enum": ["causal", "correlative"]},
                    "object": {"type": "string"}, "object_scale": scale, "object_change": change,
                    "evidence": {"type": "string", "enum": [e.id for e in fw.evidence]},
                    "context": {"type": "string"}, "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["subject", "subject_scale", "subject_change", "kind", "object", "object_scale",
                             "object_change", "evidence", "context", "quote", "confidence"],
                "additionalProperties": False,
            }},
        },
        "required": ["note_scales", "claims"],
        "additionalProperties": False,
    }


NORMALIZE_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {
        "type": "object",
        "properties": {"canonical": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}}},
        "required": ["canonical", "members"], "additionalProperties": False}}},
    "required": ["groups"],
    "additionalProperties": False,
}


def extraction_system(fw: Framework) -> str:
    return f"""You extract claims from research notes into a multi-scale model defined by this framework.

{fw.describe()}

A claim links two entities, each placed at one of the scales above, and states how a change in the \
subject goes with a change in the object. Encode perturbations and states as changes:
- "knockout of A causes X" → A decrease, X increase
- "inactivating mutations in A are found in patients with W" → A decrease, W increase (correlative)
- a condition, behaviour or disease being present or more severe → increase
Name the entity itself, not its state: words like activity, expression, loss, mutation, or \
phosphorylation belong in the change, not in the name. Use one standard name per entity and reuse it.
kind is "causal" only when the note reports evidence that the subject drives the object; otherwise \
"correlative". evidence is the type of support the note gives. context is where the claim holds \
(cell type, tissue, model, species, patient group) if stated. quote is the shortest verbatim span \
that supports the claim. confidence (0-1) reflects how firmly the note establishes it.
Extract only what the note states, not background knowledge; return no claims if there are none."""


NORMALIZE_SYSTEM = """You merge synonymous entity names in a scientific knowledge graph. Group names \
that refer to the same entity (abbreviations, spelling variants, gene vs. protein symbol for the same \
product, Greek letters spelled out). Do not merge related-but-distinct entities (family members, \
isoforms named differently, a complex vs. one subunit). For each group with more than one member pick \
the most standard name as canonical. Omit singletons."""

NARRATE_TASK = """Build a mechanism narrative for: {target}

{framework}

Pre-computed candidate chains toward the target (best first). Each step carries its sign, evidence, \
confidence after triangulation, missing intermediate scales, and open points:
{chains}

Consistency findings that touch these entities:
{consistency}

Verify the key steps against the source notes (read_note), look for mediators that fill missing \
scales (search, mechanism_entity), and consider competing chains. Then save the result with \
write_note(kind="mechanisms", ...) containing:
1. Summary — the mechanism in one paragraph.
2. Narrative — one linear account from the lowest to the highest scale, a step per transition, each \
stating the claim, its direction, evidence type, a strength label (strong / moderate / weak / \
speculative) and the notes it rests on as [[note-id]].
3. Diagram — a Mermaid flowchart of the chain (dashed arrows for correlative links).
4. Inconsistencies — incoherent loops or conflicting claims and how they might be reconciled \
(context differences, missing mediators, wrong sign).
5. Testable hypotheses — for each weak, correlative, missing or incoherent link: the hypothesis, a \
concrete experiment, the outcome predicted if the narrative is right, and what would falsify it.
Mark clearly anything you infer beyond the notes. Finish by replying with the path you wrote and a \
two-sentence summary."""


class MechanismOrganizer:
    def __init__(self, cfg: Config, store: Store, llm: Claude, fw: Framework,
                 log: Callable[[str], None] = _stderr):
        self.cfg, self.store, self.llm, self.fw, self.log = cfg, store, llm, fw, log

    def _run(self, label, system, schema, prompts):
        def work(key):
            return self.llm.structured(system, prompts[key], schema)
        return _parallel(list(prompts), work, self.cfg.max_workers, label, self.log)

    def extract(self, force: bool = False, limit: int | None = None) -> int:
        done = {r["note_id"]: (r["hash"], r["fw_hash"]) for r in self.store.db.execute(
            "SELECT note_id, hash, fw_hash FROM mech_extracted WHERE framework=?", (self.fw.id,))}
        todo = [n for n in self.store.notes() if force or done.get(n["id"]) != (n["hash"], self.fw.hash)]
        todo = todo[: limit or None]
        known = sorted(MechanismModel(self.store, self.fw).entities)
        vocab = (f"\nEntities already in the model (reuse these names when they mean the same thing):\n"
                 f"{', '.join(known[:400])}\n") if known else ""
        prompts, hashes = {}, {}
        for n in todo:
            body, more = n["body"], ""
            if len(body) > MAX_NOTE_CHARS:
                more = f"\n(Note is long; showing the first {MAX_NOTE_CHARS} of {len(body)} characters.)"
                body = body[:MAX_NOTE_CHARS]
            prompts[n["id"]] = f"{vocab}\nNote: {n['title']} ({n['path']}){more}\n\n<note>\n{body}\n</note>"
            hashes[n["id"]] = n["hash"]
        total = 0
        for nid, data in self._run("extract", extraction_system(self.fw), claim_schema(self.fw), prompts):
            total += store_claims(self.store, self.fw, nid, hashes[nid], data)
        return total

    def normalize(self, force: bool = False) -> int:
        """Merge synonymous entity names, one call per scale (only when that scale's names changed)."""
        model = MechanismModel(self.store, self.fw)
        names_by_scale: dict[str, set[str]] = defaultdict(set)
        for e in model.entities.values():  # aliases included, so earlier merges can be revisited
            names_by_scale[e.scale].update([e.name, *e.aliases])
        prompts = {}
        for scale, names in names_by_scale.items():
            if len(names) < 2:
                continue
            listing = sorted(names)[:500]
            sig = hashlib.sha256(json.dumps(listing).encode()).hexdigest()[:12]
            if not force and self.store.get_meta(f"mech_norm:{self.fw.id}:{scale}") == sig:
                continue
            prompts[scale] = (sig, f"Scale: {scale}\nNames:\n" + "\n".join(f"- {n}" for n in listing))
        merged = 0
        for scale, data in self._run("normalize", NORMALIZE_SYSTEM, NORMALIZE_SCHEMA,
                                     {k: v[1] for k, v in prompts.items()}):
            for g in data["groups"]:
                canon = g["canonical"].strip()
                for m in set(g["members"]) | {canon}:
                    self.store.db.execute("INSERT OR REPLACE INTO mech_alias VALUES (?,?,?)",
                                          (self.fw.id, norm_name(m), canon))
                    merged += 1
            self.store.db.commit()
            self.store.set_meta(f"mech_norm:{self.fw.id}:{scale}", prompts[scale][0])
        return merged

    def narrate(self, target: str | None = None, source: str | None = None,
                on_event=None) -> tuple[str, list[str]]:
        model = MechanismModel(self.store, self.fw)
        targets = [target] if target else model.default_targets(1)
        if not targets:
            return "No claims extracted yet; run `cairn mech extract` first.", []
        chains = model.chains(targets[0], source=source, k=4)
        if not chains:
            return f"No chains found ending at '{targets[0]}'.", []
        entities = {n for ch in chains for n in ch["chain"]}
        loops = [model.cycle_report(c) for c in model.cycles
                 if not c.coherent and entities & set(c.nodes)][:15]
        tools = VaultTools(self.cfg, self.store, allow_writes=True)
        task = NARRATE_TASK.format(target=targets[0], framework=self.fw.describe(),
                                   chains=json.dumps(chains, ensure_ascii=False),
                                   consistency=json.dumps(loops, ensure_ascii=False) if loops else "none found")
        answer = self.llm.run_agent(task, tools, on_event=on_event, max_turns=50)
        return answer, tools.written
