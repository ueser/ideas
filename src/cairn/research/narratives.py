"""Questions, competing narratives, distinguishing predictions, and the evidence package
an agent (or a human) works from.

A narrative is a *reading path* through the claim graph, not the graph itself: the graph can
branch, loop and skip scales; a narrative picks one linear route from a perturbation to a
phenotype. Every step carries a status:

  Supported   a causal claim in this direction
  Assumed     only association or hypothesis (direction or causation assumed)
  Challenged  the link is part of an open tension or conflicting-result case
  Missing     no claim connects the two; the gap stays visible
"""

from __future__ import annotations

import datetime as dt
import json

from ..vault import slugify
from .model import ResearchModel, state_label
from .records import DEAD, Records, short_hash

WEIGHT = {"Supported": 1.0, "Assumed": 0.5, "Challenged": 0.35, "Missing": 0.0}


def question_id(text: str) -> str:
    return "q-" + slugify(text)[:50]


def create_question(recs: Records, fw, text: str, target: str, source: str | None = None,
                    scope: str = "") -> dict:
    qid = question_id(text)
    path = recs.root / "questions" / f"{qid}.md"
    fm = {"id": qid, "type": "question", "question": text, "framework": fw.ref, "target": target,
          "source": source or "", "scope": scope or "all notes", "status": "open",
          "created": dt.date.today().isoformat()}
    body = (f"# {text}\n\nTarget: **{target}**" + (f" · from **{source}**" if source else "") +
            f"\n\nFramework: {fw.name} ({fw.ref}) · scope: {fm['scope']}\n")
    recs.write_doc(path, fm, body)
    return fm


TENSIONS = ("potential_tension", "conditional_sign_tension")


def challenged_pairs(cases: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Entity pairs with a direct conflicting-result case (opposite or null results for that link).
    Tensions challenge a narrative as a whole and are reported by `narrative_challenges`."""
    out: dict[tuple[str, str], list[dict]] = {}
    for c in cases:
        if c["type"] != "conflicting_result":
            continue
        if c.get("status", "proposed") in ("rejected",):
            continue
        ents = c["entities"]
        for i, a in enumerate(ents):
            for b in ents[i + 1:]:
                out.setdefault(tuple(sorted((a, b))), []).append(c)
    return out


def _step(model: ResearchModel, u: str, v: str, challenged: dict) -> dict:
    links = model.links_between(u, v)
    causal = [l for l in links if l.kind == "causal" and l.subj == u]
    other = [l for l in links if l.kind in ("associative", "hypothesis")]
    used = causal or other
    ids = {l.id for l in links}
    pair_cases = [c["id"] for c in challenged.get(tuple(sorted((u, v))), []) if ids & set(c["inputs"])]
    if not used:
        status = "Missing"
    elif pair_cases:
        status = "Challenged"
    elif causal:
        status = "Supported"
    else:
        status = "Assumed"
    sign = used[0].sign if used else 0
    return {
        "from": u, "to": v, "status": status, "sign": sign,
        "from_scale": model.entity_scales(u)[0], "to_scale": model.entity_scales(v)[0],
        "claims": [l.id for l in used],
        "how": "causal" if causal else (
            "hypothesis" if other and all(l.kind == "hypothesis" for l in other)
            else "association, direction assumed" if other else "no claim"),
        "assumptions": sorted({a for l in used for a in l.assumptions} | {n for l in used for n in l.notes}),
        "cases": pair_cases,
        "studies": sorted({l.study for l in used}),
    }


def narrative_challenges(steps: list[dict], cases: list[dict]) -> list[dict]:
    """Open tension cases that share claims with this narrative."""
    used = {c for s in steps for c in s["claims"]}
    return [{"id": c["id"], "type": c["type"], "title": c["title"]} for c in cases
            if c["type"] in TENSIONS and c.get("status", "proposed") != "rejected" and used & set(c["inputs"])]


def net_sign(steps: list[dict]) -> int:
    sign = 1
    for s in steps:
        if not s["sign"] or s["status"] == "Missing":
            return 0
        sign *= s["sign"]
    return sign


def _score(steps: list[dict], model: ResearchModel) -> float:
    if not steps:
        return 0.0
    scales = {s["from_scale"] for s in steps} | {steps[-1]["to_scale"]}
    return sum(WEIGHT[s["status"]] for s in steps) / len(steps) + 0.15 * len(scales) - 0.05 * len(steps)


def candidate_paths(model: ResearchModel, target: str, source: str | None, challenged: dict,
                    max_len: int = 6, cap: int = 20000) -> list[list[dict]]:
    t = model.find_entity(target)
    if t is None:
        return []
    if source:
        s = model.find_entity(source)
        starts = [s] if s else []
    else:
        mapped = [e for e in model.entities if model.level(e) >= 0 and e != t]
        low = min((model.level(e) for e in mapped), default=0)
        starts = [e for e in mapped if model.level(e) == low]
    found: list[list[str]] = []

    def dfs(path):
        if len(found) >= cap:
            return
        if path[-1] == t and len(path) > 1:
            found.append(list(path))
            return
        if len(path) > max_len:
            return
        for nxt, _, _ in model.step_options(path[-1]):
            if nxt not in path:
                path.append(nxt)
                dfs(path)
                path.pop()

    for s in starts:
        dfs([s])
    paths = [[_step(model, u, v, challenged) for u, v in zip(p, p[1:])] for p in found]
    if not paths and starts:
        # the target is unreachable: go as far up as the evidence allows, then show the gap
        best = None
        for s in starts:
            reach = _reachable(model, s, max_len)
            if reach:
                end = max(reach, key=lambda e: (model.level(e), -len(reach[e])))
                cand = reach[end]
                if best is None or model.level(cand[-1]) > model.level(best[-1]):
                    best = cand
        partial = best or [starts[0]]
        steps = [_step(model, u, v, challenged) for u, v in zip(partial, partial[1:])]
        steps.append(_step(model, partial[-1], t, challenged))
        paths = [steps]
    paths.sort(key=lambda st: -_score(st, model))
    return paths


def _reachable(model: ResearchModel, start: str, max_len: int) -> dict[str, list[str]]:
    seen = {start: [start]}
    frontier = [start]
    for _ in range(max_len):
        nxt = []
        for u in frontier:
            for v, _, _ in model.step_options(u):
                if v not in seen:
                    seen[v] = seen[u] + [v]
                    nxt.append(v)
        frontier = nxt
    return seen


def competing(paths: list[list[dict]], k: int = 2, max_overlap: float = 0.5) -> list[list[dict]]:
    chosen = []
    for p in paths:
        edges = {(s["from"], s["to"]) for s in p}
        if all(len(edges & {(s["from"], s["to"]) for s in q}) / max(len(edges), 1) <= max_overlap for q in chosen):
            chosen.append(p)
        if len(chosen) == k:
            break
    return chosen


def predictions(a: list[dict], b: list[dict], source: str, target: str) -> list[dict]:
    """Observations whose expected outcome differs between narratives A and B:
    the source's net effect on the target, and mediation by intermediates only one narrative uses."""
    out = []
    word = {1: "rise", -1: "fall"}
    sa, sb = net_sign(a), net_sign(b)
    src_a, src_b = a[0]["from"], b[0]["from"]
    if src_a == src_b and sa and sb and sa != sb:
        out.append({"observation": f"Reduce {src_a} and measure {target}",
                    "if_A": f"{target} should {word[-sa]}", "if_B": f"{target} should {word[-sb]}",
                    "test": f"A matched-context perturbation of {src_a} with a {target} readout",
                    "falsifies": f"A if {target} {word[-sb]}s; B if it {word[-sa]}s"})
    mids_a = [s["to"] for s in a[:-1]]
    mids_b = [s["to"] for s in b[:-1]]
    for mine, theirs, me, other, src in ((mids_a, mids_b, "A", "B", src_a), (mids_b, mids_a, "B", "A", src_b)):
        for e in mine:
            if e in theirs or e in (src_a, src_b, target):
                continue
            out.append({"observation": f"Block {e}, then change {src} and measure {target}",
                        f"if_{me}": f"the effect of {src} on {target} is lost or much reduced",
                        f"if_{other}": f"the effect persists ({other} does not pass through {e})",
                        "test": f"Mediation test: perturb {src} with and without blocking {e}",
                        "falsifies": f"{me} if the effect persists; {other} if it is lost"})
    return out[:6]


def coverage(model: ResearchModel, steps: list[dict]) -> dict:
    used = {s["from_scale"] for s in steps} | ({steps[-1]["to_scale"]} if steps else set())
    return {"scales_used": [s for s in model.fw.scale_ids if s in used],
            "scales_not_used": [s for s in model.fw.scale_ids if s not in used],
            "note": "Scales not used may be irrelevant to this question; they are listed, not counted as gaps."}


def compose_context(model: ResearchModel, question: dict, cases: list[dict], max_claims: int = 60) -> dict:
    """The evidence package for an investigation: what a human or an agent should look at."""
    challenged = challenged_pairs(cases)
    paths = candidate_paths(model, question["target"], question.get("source") or None, challenged)
    narr = competing(paths)
    on_path = [cid for p in narr for s in p for cid in s["claims"]]
    ents = {e for p in narr for s in p for e in (s["from"], s["to"])}
    rel_cases = [c for c in cases if set(c["entities"]) & ents]
    case_claims = [cid for c in rel_cases for cid in c["inputs"]]
    selected = list(dict.fromkeys(on_path + case_claims))[:max_claims]
    counter = sorted({l.id for l in model.links if l.kind == "null" and {l.subj, l.obj} & ents} |
                     {i for c in rel_cases if c["type"] != "triangulation" for i in c["inputs"]} - set(on_path))
    excluded = [{"id": c["id"], "status": c["status"], "reason": c.get("status_reason", "")}
                for c in model.excluded
                if {model.canonical(c["subject"]["entity"]), model.canonical(c["object"]["entity"])} & ents]
    unmapped = sorted({e for e in model.entities if model.entity_scales(e) == ["unmapped"]})

    def claim_view(cid):
        c = model.claims[cid]
        return {"id": cid, "status": c["status"], "note": c["note"],
                "lines": f"{c['span']['line_start']}-{c['span']['line_end']}", "passage": c["span"]["text"],
                "claim": f"{state_label(c['subject'])} —{c['predicate']}{' (negated)' if c['negated'] else ''}→ "
                         f"{state_label(c['object'])}", "type": c["claim_type"], "evidence": c["evidence"],
                "context": {k: v for k, v in c["context"].items() if v}, "study": c["study"]["id"]}
    pkg = {
        "question": question["question"], "question_id": question["id"], "target": question["target"],
        "source": question.get("source") or None, "scope": question.get("scope", "all notes"),
        "framework": model.fw.ref,
        "records_revision": short_hash(sorted((c["id"], c["status"]) for c in model.records.all_claims())),
        "narratives": [{"label": "AB"[i], "steps": p, "coverage": coverage(model, p), "net_sign": net_sign(p),
                        "challenges": narrative_challenges(p, cases)} for i, p in enumerate(narr)],
        "claims": [claim_view(c) for c in selected],
        "counterevidence": [claim_view(c) for c in counter if c in model.claims][:20],
        "cases": [{"id": c["id"], "type": c["type"], "summary": c["summary"],
                   "status": c.get("status", "proposed")} for c in rel_cases],
        "gaps": [f"{s['from']} → {s['to']}" for p in narr for s in p if s["status"] == "Missing"] +
                [c["title"] for c in rel_cases if c["type"] == "missing_bridge"],
        "assumptions": sorted({a for p in narr for s in p for a in s["assumptions"]}),
        "exclusions": {"claims_not_live": excluded[:30], "unmapped_entities": unmapped[:30]},
    }
    pkg["token_estimate"] = len(json.dumps(pkg)) // 4
    return pkg


# ---- LLM synthesis + mechanical validation ------------------------------------------

STATUS = ["Supported", "Assumed", "Challenged", "Missing"]

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "narratives": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"},
                "steps": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"from": {"type": "string"}, "to": {"type": "string"},
                                   "status": {"type": "string", "enum": STATUS},
                                   "claims": {"type": "array", "items": {"type": "string"}},
                                   "text": {"type": "string"}},
                    "required": ["from", "to", "status", "claims", "text"], "additionalProperties": False}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["label", "title", "summary", "steps", "assumptions"], "additionalProperties": False}},
        "predictions": {"type": "array", "items": {
            "type": "object",
            "properties": {"observation": {"type": "string"}, "if_A": {"type": "string"}, "if_B": {"type": "string"},
                           "test": {"type": "string"}, "falsifies": {"type": "string"}},
            "required": ["observation", "if_A", "if_B", "test", "falsifies"], "additionalProperties": False}},
    },
    "required": ["narratives", "predictions"], "additionalProperties": False,
}

SYNTH_SYSTEM = """You write competing mechanism narratives from a prepared evidence package. Each \
narrative is one linear reading path from a perturbation to the target phenotype. Use only the \
claims in the package and cite them by id on every step. Keep each step's status honest: \
"Supported" only for a causal claim in that direction; "Assumed" where the direction or causation is \
assumed; "Challenged" where a case disputes the link; "Missing" where no claim exists — never fill a \
gap from background knowledge. Narrative B should be the strongest genuine alternative (a different \
route, or the same observations explained differently, e.g. a response rather than a cause), not a \
variant of A. Predictions are observations whose expected outcome differs between A and B, each with \
a test and what result would falsify which narrative. These are research hypotheses, not protocols."""


def validate(model: ResearchModel, narrative: dict) -> list[str]:
    """Mechanical checks on a synthesized narrative; fixes statuses it cannot justify."""
    issues = []
    for i, st in enumerate(narrative["steps"], 1):
        u, v = model.find_entity(st["from"]), model.find_entity(st["to"])
        cited = [c for c in st["claims"] if c in model.claims]
        unknown = [c for c in st["claims"] if c not in model.claims]
        if unknown:
            issues.append(f"step {i}: cites unknown claim(s) {', '.join(unknown)} (removed)")
        dead = [c for c in cited if model.claims[c]["status"] in DEAD]
        if dead:
            issues.append(f"step {i}: cites claims that are not live: {', '.join(dead)}")
        live_links = [l for l in model.links if l.id in cited]
        off = [l.id for l in live_links if {l.subj, l.obj} != {u, v}]
        if off:
            issues.append(f"step {i}: {', '.join(off)} do not connect {st['from']} and {st['to']}")
        connecting = [l for l in live_links if {l.subj, l.obj} == {u, v}]
        st["claims"] = [c for c in cited if c not in off]
        if not connecting and st["status"] != "Missing":
            issues.append(f"step {i}: no claim connects {st['from']} → {st['to']}; status set to Missing")
            st["status"] = "Missing"
        elif st["status"] == "Supported" and not any(l.kind == "causal" and l.subj == u for l in connecting):
            issues.append(f"step {i}: no causal claim in this direction; status set to Assumed")
            st["status"] = "Assumed"
    return issues


def synthesize(llm, model: ResearchModel, package: dict) -> dict:
    prompt = ("Evidence package (JSON):\n" + json.dumps(package, ensure_ascii=False) +
              "\n\nWrite narratives A and B and the distinguishing predictions.")
    out = llm.structured(SYNTH_SYSTEM, prompt, SYNTH_SCHEMA, effort=llm.cfg.agent_effort)
    for n in out["narratives"]:
        n["validation"] = validate(model, n)
    return out
