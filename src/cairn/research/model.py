"""In-memory projection of the claim records under one framework.

Signs. A participant is an entity state: entity + property + direction (e.g. KRN1 / activity /
decrease; KRN1 / loss-of-function variant / present). Its *effect* is how that state moves the
entity's activity or level: stated in the passage, inferred by the extractor (an assumption),
or taken from the direction. When both effects are known the claim's sign is their product;
otherwise the relation type's sign fills in. "Loss of A raises X" is sign -1 between A and X.

A claim counts as causal only if it is worded causally, its relation type is causal, AND its
evidence type can support causality. Otherwise it is used as an association, never composed
as an effect.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .framework import Framework
from .records import LIVE, Records

GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "κ": "kappa", "λ": "lambda",
         "μ": "mu", "σ": "sigma", "τ": "tau", "ω": "omega"}
EFFECT = {"increase": 1, "decrease": -1}
DIRECTION = {"increase": 1, "present": 1, "decrease": -1, "absent": -1}


def norm_name(name: str) -> str:
    s = name.strip().lower()
    for g, latin in GREEK.items():
        s = s.replace(g, latin)
    return re.sub(r"[^a-z0-9]+", "", s)


def effect(p: dict) -> tuple[int, str]:
    """(sign, basis) for a participant; basis is 'stated', 'inferred' or 'direction'."""
    e = EFFECT.get(p.get("activity_effect", ""))
    if e:
        return e, p.get("effect_basis", "stated")
    return DIRECTION.get(p.get("direction", ""), 0), "direction"


def state_label(p: dict) -> str:
    d = {"increase": "↑", "decrease": "↓", "present": "present", "absent": "absent"}.get(p.get("direction", ""), "")
    prop = p.get("property", "")
    if prop and prop.lower() in p["entity"].lower():
        prop = ""  # "myofibroblast activation activation" -> "myofibroblast activation"
    return " ".join(x for x in (p["entity"], prop, d) if x)


@dataclass
class Link:
    claim: dict
    subj: str  # canonical entity names
    obj: str
    sign: int
    kind: str  # causal | associative | hypothesis | structural | null
    notes: list[str] = field(default_factory=list)  # demotions, sign disagreements
    assumptions: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.claim["id"]

    @property
    def study(self) -> str:
        return self.claim["study"]["id"]

    def other(self, entity: str) -> str:
        return self.obj if entity == self.subj else self.subj


class ResearchModel:
    def __init__(self, records: Records, fw: Framework, statuses=LIVE):
        self.records, self.fw = records, fw
        self.aliases = records.alias_map()
        all_claims = records.all_claims()
        self.claims = {c["id"]: c for c in all_claims}
        self.live = [c for c in all_claims if c["status"] in statuses and c["framework"].split("@")[0] == fw.id]
        self.excluded = [c for c in all_claims if c not in self.live]
        self.mappings = records.mappings(fw.id)
        self.links: list[Link] = [self._link(c) for c in self.live]
        self.pairs: dict[tuple[str, str], list[Link]] = defaultdict(list)
        self.entities: dict[str, dict] = {}
        for link in self.links:
            for role, name in (("subject", link.subj), ("object", link.obj)):
                e = self.entities.setdefault(name, {"name": name, "states": Counter(), "notes": set(),
                                                    "claims": [], "aliases": set()})
                p = link.claim[role]
                e["states"][p.get("property", "")] += 1
                e["notes"].add(link.claim["note"])
                e["claims"].append(link.id)
                if p["entity"] != name:
                    e["aliases"].add(p["entity"])
            if link.subj != link.obj:
                self.pairs[tuple(sorted((link.subj, link.obj)))].append(link)
        self.adj: dict[str, set[str]] = defaultdict(set)
        for a, b in self.pairs:
            self.adj[a].add(b)
            self.adj[b].add(a)

    # ---- construction -----------------------------------------------------
    def canonical(self, name: str) -> str:
        return self.aliases.get(norm_name(name), name.strip())

    def _link(self, c: dict) -> Link:
        rel = self.fw.relation(c["predicate"])
        ev = self.fw.evidence_type(c["evidence"])
        s, o = self.canonical(c["subject"]["entity"]), self.canonical(c["object"]["entity"])
        notes, assumptions = [], []
        es, bs = effect(c["subject"])
        eo, bo = effect(c["object"])
        for p, basis in ((c["subject"], bs), (c["object"], bo)):
            if basis == "inferred":
                assumptions.append(f"assumes {p['property']} of {p['entity']} "
                                   f"{'lowers' if effect(p)[0] < 0 else 'raises'} its activity (inferred, not stated)")
        rsign = rel.sign if rel else 0
        if es and eo:
            sign = es * eo
            if rsign and rsign != sign:
                notes.append(f"relation '{c['predicate']}' disagrees with the stated directions; directions used")
        elif rsign:
            sign = rsign * (es or 1) * (eo or 1)
        else:
            sign = 0
        if c.get("negated"):
            kind = "null"
        elif rel is None or rel.kind in ("compositional", "descriptive"):
            kind = "structural"
        elif c["claim_type"] == "hypothesis":
            kind = "hypothesis"
        elif rel.kind == "causal" and c["claim_type"] == "causal" and ev and ev.supports_causal:
            kind = "causal"
        else:
            kind = "associative"
            if rel.kind == "causal" or c["claim_type"] == "causal":
                notes.append(f"causal wording, but {c['evidence']} evidence cannot establish causation: "
                             "treated as an association")
        return Link(c, s, o, sign, kind, notes, assumptions)

    # ---- framework mapping -----------------------------------------------
    def state_key(self, entity: str, prop: str) -> str:
        return f"{self.canonical(entity)}|{(prop or '').strip().lower()}"

    def entity_scales(self, entity: str) -> list[str]:
        votes: Counter = Counter()
        for m in self.mappings.values():
            if self.canonical(m["entity"]) == entity and m.get("status") not in ("rejected",):
                for s in m["scales"]:
                    votes[s] += m.get("votes", 1)
        return [s for s, _ in votes.most_common() if s in self.fw.scale_ids] or ["unmapped"]

    def level(self, entity: str) -> int:
        return self.fw.scale_index(self.entity_scales(entity)[0])

    # ---- queries ------------------------------------------------------------
    def find_entity(self, name: str) -> str | None:
        if name in self.entities:
            return name
        key = norm_name(self.canonical(name))
        for e in self.entities.values():
            if norm_name(e["name"]) == key or key in {norm_name(a) for a in e["aliases"]}:
                return e["name"]
        return None

    def links_between(self, a: str, b: str) -> list[Link]:
        return self.pairs.get(tuple(sorted((a, b))), [])

    def context_compare(self, claims: list[dict]) -> dict:
        """Compare the framework's must-match context fields across claims."""
        mismatched, unknown = {}, defaultdict(list)
        for f in self.fw.context_match:
            values = {}
            for c in claims:
                v = (c.get("context") or {}).get(f)
                if v in (None, "", "unknown"):
                    unknown[f].append(c["id"])
                else:
                    values.setdefault(str(v).strip().lower(), []).append(c["id"])
            if len(values) > 1:
                mismatched[f] = {v: ids for v, ids in values.items()}
        verdict = "mismatch" if mismatched else ("unknown" if unknown else "compatible")
        return {"verdict": verdict, "mismatched": mismatched, "unknown": dict(unknown)}

    def step_options(self, entity: str) -> list[tuple[str, Link, str]]:
        """Directed reading steps out of `entity`: (next entity, link, how)."""
        out = []
        for other in self.adj[entity]:
            for link in self.links_between(entity, other):
                if link.kind == "causal" and link.subj == entity:
                    out.append((other, link, "causal"))
                elif link.kind == "associative":  # an association can be read either way, as an assumption
                    out.append((other, link, "assumed-direction" if link.subj != entity else link.kind))
                elif link.kind == "hypothesis" and link.subj == entity:  # a proposal keeps its direction
                    out.append((other, link, "hypothesis"))
        return out
