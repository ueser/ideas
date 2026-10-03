"""Mechanism model: a signed, multi-scale graph built from claims extracted
under a framework, with triangulation (cycle balance) and chain finding.

Signs. Every claim says how a change in the subject goes with a change in the
object: "loss of A leads to X" is (A: decrease) -> (X: increase), sign -1.
Around any loop of claims the product of signs must be +1 for the loop to be
coherent (a balanced cycle in a signed graph). Example from the framework:

    A activity -> cell behaviour X     (+)
    X observed in condition W          (+)
    W correlates with A inactivation   (-)      product = -1  => incoherent

Coherent loops raise confidence in their edges; incoherent loops flag them.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .framework import Framework
from .store import Store

CHANGE_SIGN = {"increase": 1, "decrease": -1, "unclear": 0}
GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "κ": "kappa", "λ": "lambda",
         "μ": "mu", "σ": "sigma", "τ": "tau", "ω": "omega"}


def norm_name(name: str) -> str:
    s = name.strip().lower()
    for g, latin in GREEK.items():
        s = s.replace(g, latin)
    return re.sub(r"[^a-z0-9]+", "", s)


@dataclass
class Claim:
    id: int
    note_id: str
    subject: str
    subject_scale: str
    subject_change: str
    kind: str  # causal | correlative
    object: str
    object_scale: str
    object_change: str
    evidence: str
    context: str
    quote: str
    confidence: float
    weight: float = 0.0  # evidence weight x confidence, set by the model

    @property
    def sign(self) -> int:
        return CHANGE_SIGN.get(self.subject_change, 0) * CHANGE_SIGN.get(self.object_change, 0)


@dataclass
class Entity:
    name: str
    scale: str
    aliases: set[str] = field(default_factory=set)
    notes: set[str] = field(default_factory=set)
    claim_ids: list[int] = field(default_factory=list)


@dataclass
class Pair:
    """All claims between two entities, in either direction."""
    a: str
    b: str
    claims: list[Claim] = field(default_factory=list)
    coherent: int = 0
    incoherent: int = 0

    def _support(self, sign: int) -> float:
        # noisy-OR over notes: independent sources accumulate, repeats within one note don't
        best: dict[str, float] = {}
        for c in self.claims:
            if c.sign == sign:
                best[c.note_id] = max(best.get(c.note_id, 0.0), c.weight)
        return 1.0 - math.prod(1.0 - w for w in best.values())

    @property
    def plus(self) -> float:
        return self._support(1)

    @property
    def minus(self) -> float:
        return self._support(-1)

    @property
    def contested(self) -> bool:
        p, m = self.plus, self.minus
        return p > 0 and m > 0 and min(p, m) > 0.5 * max(p, m)

    @property
    def sign(self) -> int:
        p, m = self.plus, self.minus
        if self.contested or (p == 0 and m == 0):
            return 0
        return 1 if p > m else -1

    @property
    def evidence(self) -> float:
        return max(self.plus, self.minus, self._support(0))

    @property
    def confidence(self) -> float:
        """Evidence strength adjusted by triangulation."""
        c = self.evidence * (1 + 0.15 * self.coherent) / (1 + 0.3 * self.incoherent)
        return round(min(c, 1.0), 3)

    @property
    def causal_dirs(self) -> set[tuple[str, str]]:
        return {(c.subject, c.object) for c in self.claims if c.kind == "causal"}

    @property
    def sources(self) -> list[str]:
        return sorted({c.note_id for c in self.claims})


@dataclass
class Cycle:
    nodes: list[str]
    signs: list[int]  # sign of edge nodes[i] -- nodes[i+1] (wrapping)

    @property
    def coherent(self) -> bool:
        return math.prod(self.signs) > 0


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


class MechanismModel:
    def __init__(self, store: Store, fw: Framework, max_cycle_len: int = 4, max_cycles: int = 20000):
        self.store, self.fw = store, fw
        self.aliases = {r["alias"]: r["canonical"] for r in store.db.execute(
            "SELECT alias, canonical FROM mech_alias WHERE framework=?", (fw.id,))}
        self.claims: list[Claim] = []
        for r in store.db.execute("SELECT rowid AS id, * FROM mech_claims WHERE framework=? ORDER BY rowid", (fw.id,)):
            d = {k: r[k] for k in r.keys() if k != "framework"}
            c = Claim(**d)
            c.weight = fw.evidence_weight(c.evidence) * max(0.0, min(1.0, c.confidence or 0.0))
            self.claims.append(c)
        self._build_entities()
        self.pairs: dict[tuple[str, str], Pair] = {}
        for c in self.claims:
            if c.subject == c.object:
                continue
            k = _key(c.subject, c.object)
            self.pairs.setdefault(k, Pair(*k)).claims.append(c)
        self.adj: dict[str, set[str]] = defaultdict(set)
        for a, b in self.pairs:
            self.adj[a].add(b)
            self.adj[b].add(a)
        self.cycles = self._find_cycles(max_cycle_len, max_cycles)
        for cyc in self.cycles:
            n = len(cyc.nodes)
            for i in range(n):
                p = self.pairs[_key(cyc.nodes[i], cyc.nodes[(i + 1) % n])]
                if cyc.coherent:
                    p.coherent += 1
                else:
                    p.incoherent += 1

    # ---- entities -------------------------------------------------------
    def canonical(self, name: str) -> str:
        return self.aliases.get(norm_name(name), name.strip())

    def _build_entities(self) -> None:
        surface: dict[str, Counter] = defaultdict(Counter)  # canonical key -> surface forms
        scales: dict[str, Counter] = defaultdict(Counter)
        for c in self.claims:
            for attr in ("subject", "object"):
                raw = getattr(c, attr)
                canon = self.canonical(raw)
                key = norm_name(canon)
                surface[key][canon] += 1
                surface[key][raw] += 0  # remember as alias
                scales[key][getattr(c, f"{attr}_scale")] += 1
        names = {k: cnt.most_common(1)[0][0] for k, cnt in surface.items()}
        self.entities: dict[str, Entity] = {}
        for k, name in names.items():
            scale = max(scales[k].items(), key=lambda kv: (kv[1], -self.fw.scale_index(kv[0])))[0]
            self.entities[name] = Entity(name, scale, aliases=set(surface[k]) - {name})
        for c in self.claims:
            c.subject = names[norm_name(self.canonical(c.subject))]
            c.object = names[norm_name(self.canonical(c.object))]
            for n in (c.subject, c.object):
                self.entities[n].notes.add(c.note_id)
                self.entities[n].claim_ids.append(c.id)

    def find_entity(self, name: str) -> Entity | None:
        if name in self.entities:
            return self.entities[name]
        key = norm_name(self.canonical(name))
        for e in self.entities.values():
            if norm_name(e.name) == key or key in {norm_name(a) for a in e.aliases}:
                return e
        return None

    def level(self, entity: str) -> int:
        return self.fw.scale_index(self.entities[entity].scale)

    # ---- triangulation ---------------------------------------------------
    def _find_cycles(self, max_len: int, max_cycles: int) -> list[Cycle]:
        """Simple cycles of length 3..max_len over signed pairs; each found once
        (start at its smallest node, second node < last node)."""
        signed = {k for k, p in self.pairs.items() if p.sign != 0}
        nbrs: dict[str, list[str]] = defaultdict(list)
        for a, b in signed:
            nbrs[a].append(b)
            nbrs[b].append(a)
        for v in nbrs:
            nbrs[v].sort()
        cycles: list[Cycle] = []

        def dfs(start: str, path: list[str]) -> None:
            if len(cycles) >= max_cycles:
                return
            last = path[-1]
            for u in nbrs[last]:
                if u == start and len(path) >= 3 and path[1] < path[-1]:
                    signs = [self.pairs[_key(path[i], path[(i + 1) % len(path)])].sign for i in range(len(path))]
                    cycles.append(Cycle(list(path), signs))
                elif u > start and u not in path and len(path) < max_len:
                    path.append(u)
                    dfs(start, path)
                    path.pop()

        for s in sorted(nbrs):
            dfs(s, [s])
        return cycles

    def cycle_report(self, cyc: Cycle) -> dict:
        n = len(cyc.nodes)
        edges = []
        for i in range(n):
            a, b = cyc.nodes[i], cyc.nodes[(i + 1) % n]
            p = self.pairs[_key(a, b)]
            edges.append({"between": [a, b], "sign": "+" if p.sign > 0 else "-", "evidence": round(p.evidence, 2),
                          "_rank": (-(p.incoherent - p.coherent), p.evidence),
                          "kinds": sorted({c.kind for c in p.claims}), "sources": p.sources,
                          "contexts": sorted({c.context for c in p.claims if c.context})})
        suspect = min(edges, key=lambda e: e["_rank"])["between"]
        for e in edges:
            del e["_rank"]
        out = {"loop": cyc.nodes + [cyc.nodes[0]], "coherent": cyc.coherent, "edges": edges}
        if not cyc.coherent:
            # the link in the most other incoherent (and fewest coherent) loops, then the least evidenced
            out["suspect_link"] = suspect
            out["note"] = ("Signs multiply to '-': at least one link is wrong, context-dependent (compare contexts), "
                           "or the loop hides a missing mediator.")
        return out

    # ---- chains -----------------------------------------------------------
    def _steps_into(self, v: str) -> list[tuple[str, Pair, str]]:
        """Predecessors u of v usable in an upward chain u -> v, with the step kind."""
        out = []
        for u in self.adj[v]:
            if self.level(u) > self.level(v):
                continue
            p = self.pairs[_key(u, v)]
            if (u, v) in p.causal_dirs:
                out.append((u, p, "causal"))
            elif any(c.kind == "correlative" for c in p.claims):
                out.append((u, p, "correlative"))
        return out

    def _score(self, path: list[str], kinds: list[str], pairs: list[Pair]) -> float:
        levels = [self.level(n) for n in path]
        distinct = len(set(levels))
        gaps = sum(max(0, b - a - 1) for a, b in zip(levels, levels[1:]))
        conf = sum(p.confidence * (1.0 if k == "causal" else 0.6) for p, k in zip(pairs, kinds)) / max(len(pairs), 1)
        incoh = sum(1 for p in pairs if p.incoherent)
        return distinct - 0.3 * gaps + 2.0 * conf - 0.5 * incoh

    def chains(self, target: str, source: str | None = None, k: int = 5, max_len: int = 9,
               beam: int = 400) -> list[dict]:
        """Best upward chains (low scale -> high scale) that end at `target`."""
        t = self.find_entity(target)
        if t is None:
            return []
        s = self.find_entity(source) if source else None
        frontier = [([t.name], [], [])]  # nodes (low->high), step kinds, pairs
        done = []
        while frontier:
            nxt = []
            for nodes, kinds, pairs in frontier:
                if len(nodes) > 1 and (s is None or nodes[0] == s.name):
                    done.append((self._score(nodes, kinds, pairs), nodes, kinds, pairs))
                if len(nodes) >= max_len:
                    continue
                for u, p, kind in self._steps_into(nodes[0]):
                    if u not in nodes:
                        nxt.append(([u] + nodes, [kind] + kinds, [p] + pairs))
            nxt.sort(key=lambda x: -self._score(*x))
            frontier = nxt[:beam]
        done.sort(key=lambda x: -x[0])
        chosen: list[tuple] = []
        for item in done:
            nodes = item[1]
            # skip chains contained in a better one already chosen
            if any(set(nodes) <= set(c[1]) for c in chosen):
                continue
            chosen.append(item)
            if len(chosen) >= k:
                break
        return [self.chain_report(nodes, kinds, pairs, score) for score, nodes, kinds, pairs in chosen]

    def chain_report(self, nodes: list[str], kinds: list[str], pairs: list[Pair], score: float) -> dict:
        steps, open_points = [], []
        for (u, v), kind, p in zip(zip(nodes, nodes[1:]), kinds, pairs):
            lu, lv = self.level(u), self.level(v)
            missing = [self.fw.scales[i].id for i in range(lu + 1, lv)]
            step = {"from": u, "to": v, "from_scale": self.entities[u].scale, "to_scale": self.entities[v].scale,
                    "kind": kind, "sign": {1: "+", -1: "-", 0: "?"}[p.sign], "confidence": p.confidence,
                    "contested": p.contested, "coherent_loops": p.coherent, "incoherent_loops": p.incoherent,
                    "sources": p.sources, "missing_scales": missing,
                    "evidence": [{"note": c.note_id, "claim": f"{c.subject} {c.subject_change} → {c.object} "
                                  f"{c.object_change}", "type": c.evidence, "context": c.context, "quote": c.quote}
                                 for c in p.claims[:4]]}
            steps.append(step)
            if kind == "correlative":
                open_points.append(f"{u} → {v} is only correlative: perturb {u} and measure {v}.")
            if missing:
                open_points.append(f"{u} → {v} skips {', '.join(missing)}: identify the mediator(s).")
            if p.contested:
                open_points.append(f"{u} – {v} has conflicting signs across notes: compare contexts.")
            if p.incoherent:
                open_points.append(f"{u} – {v} sits in {p.incoherent} incoherent loop(s).")
            if p.evidence < 0.4:
                open_points.append(f"{u} → {v} rests on weak evidence ({p.evidence:.2f}): replicate.")
        covered = sorted({self.entities[n].scale for n in nodes}, key=self.fw.scale_index)
        return {"chain": nodes, "score": round(score, 2), "scales_covered": covered,
                "steps": steps, "open_points": open_points}

    # ---- reporting -----------------------------------------------------
    def entity_report(self, name: str) -> dict | None:
        e = self.find_entity(name)
        if e is None:
            return None
        up, down, assoc = [], [], []
        for other in sorted(self.adj[e.name]):
            p = self.pairs[_key(e.name, other)]
            item = {"entity": other, "scale": self.entities[other].scale, "sign": {1: "+", -1: "-", 0: "?"}[p.sign],
                    "confidence": p.confidence, "contested": p.contested, "sources": p.sources,
                    "claims": [{"note": c.note_id, "text": f"{c.subject} {c.subject_change} → {c.object} "
                                f"{c.object_change}", "kind": c.kind, "evidence": c.evidence, "context": c.context,
                                "quote": c.quote} for c in p.claims[:6]]}
            if (other, e.name) in p.causal_dirs:
                up.append(item)
            elif (e.name, other) in p.causal_dirs:
                down.append(item)
            else:
                assoc.append(item)
        loops = [self.cycle_report(c) for c in self.cycles if e.name in c.nodes]
        return {"name": e.name, "scale": e.scale, "aliases": sorted(e.aliases), "notes": sorted(e.notes),
                "upstream": up, "downstream": down, "associations": assoc,
                "incoherent_loops": [l for l in loops if not l["coherent"]][:10],
                "coherent_loops": len([l for l in loops if l["coherent"]])}

    def consistency(self, entity: str | None = None, limit: int = 30) -> dict:
        cycles = self.cycles
        if entity:
            e = self.find_entity(entity)
            cycles = [c for c in cycles if e and e.name in c.nodes]
        bad = [c for c in cycles if not c.coherent]
        contested = [p for p in self.pairs.values() if p.contested]
        # a link that sits in many incoherent loops and few coherent ones is the likeliest culprit
        suspects = sorted((p for p in self.pairs.values() if p.incoherent),
                          key=lambda p: (-(p.incoherent - p.coherent), p.evidence))
        return {
            "loops_checked": len(cycles), "coherent": len(cycles) - len(bad), "incoherent": len(bad),
            "suspect_links": [{"between": [p.a, p.b], "incoherent_loops": p.incoherent,
                               "coherent_loops": p.coherent, "evidence": round(p.evidence, 2),
                               "kinds": sorted({c.kind for c in p.claims}),
                               "contexts": sorted({c.context for c in p.claims if c.context}),
                               "sources": p.sources} for p in suspects][:limit],
            "incoherent_loops": [self.cycle_report(c) for c in sorted(bad, key=lambda c: len(c.nodes))[:limit]],
            "contested_links": [{"between": [p.a, p.b], "support_plus": round(p.plus, 2),
                                 "support_minus": round(p.minus, 2), "sources": p.sources} for p in contested][:limit],
            "best_triangulated": [{"between": [p.a, p.b], "coherent_loops": p.coherent, "confidence": p.confidence}
                                  for p in sorted(self.pairs.values(), key=lambda p: -p.coherent)
                                  if p.coherent and not p.incoherent][:limit],
        }

    def overview(self) -> dict:
        by_scale = defaultdict(list)
        for e in self.entities.values():
            by_scale[e.scale].append(e)
        top = []
        for s in self.fw.scales:
            ents = sorted(by_scale.get(s.id, []), key=lambda e: (-len(self.adj[e.name]), e.name))
            top.append({"scale": s.id, "name": s.name, "entities": len(ents),
                        "top": [e.name for e in ents[:8]]})
        incoh = sum(1 for c in self.cycles if not c.coherent)
        return {"framework": self.fw.name, "goal": self.fw.goal, "claims": len(self.claims),
                "entities": len(self.entities), "links": len(self.pairs), "scales": top,
                "loops_checked": len(self.cycles), "incoherent_loops": incoh,
                "targets": self.default_targets()}

    def default_targets(self, k: int = 5) -> list[str]:
        """Highest-scale entities with the most connections: natural chain endpoints."""
        if not self.entities:
            return []
        top_level = max(self.level(n) for n in self.entities)
        ents = [e for e in self.entities.values() if self.level(e.name) == top_level]
        return [e.name for e in sorted(ents, key=lambda e: (-len(self.adj[e.name]), e.name))[:k]]

    def note_entities(self, note_id: str) -> list[str]:
        return sorted(e.name for e in self.entities.values() if note_id in e.notes)


def store_claims(store: Store, fw: Framework, note_id: str, note_hash: str, data: dict) -> int:
    db = store.db
    db.execute("DELETE FROM mech_claims WHERE framework=? AND note_id=?", (fw.id, note_id))
    valid = set(fw.scale_ids)
    evidence = {e.id for e in fw.evidence}
    n = 0
    for c in data.get("claims", []):
        if c["subject_scale"] not in valid or c["object_scale"] not in valid:
            continue
        if not c["subject"].strip() or not c["object"].strip():
            continue
        db.execute("INSERT INTO mech_claims (framework, note_id, subject, subject_scale, subject_change, kind, "
                   "object, object_scale, object_change, evidence, context, quote, confidence) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (fw.id, note_id, c["subject"].strip(), c["subject_scale"], c["subject_change"], c["kind"],
                    c["object"].strip(), c["object_scale"], c["object_change"],
                    c["evidence"] if c["evidence"] in evidence else "stated", c.get("context", ""),
                    c.get("quote", ""), float(c.get("confidence", 0.5))))
        n += 1
    db.execute("INSERT OR REPLACE INTO mech_extracted VALUES (?,?,?,?,?)",
               (fw.id, note_id, note_hash, fw.hash, json.dumps([s for s in data.get("note_scales", []) if s in valid])))
    db.commit()
    return n
