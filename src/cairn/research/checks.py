"""Reasoning checks. Each produces *cases* for review, never scores.

  conditional_sign_tension  a loop whose signs multiply to '-'. Labelled a *potential* tension
                            unless every link is causal; lists missing premises (associations
                            standing in for effects), context and property assumptions, and
                            competing explanations with distinguishing evidence.
  conflicting_result        the same pair reported with opposite signs, or with no effect,
                            by different studies.
  independent_evidence      positive triangulation: which claim is strengthened, by which
                            *independent studies* (not notes), under which assumptions.
  missing_bridge            a top-scale entity (e.g. a disease) connected only by association.

A closed loop on its own adds no evidence, so nothing here changes a confidence value.
"""

from __future__ import annotations

from collections import defaultdict

from .model import Link, ResearchModel, state_label
from .records import short_hash

UP_DOWN = {1: "rise", -1: "fall"}


def _case(ctype: str, title: str, links: list[Link], model: ResearchModel, **fields) -> dict:
    inputs = sorted({l.id for l in links})
    return {
        "id": f"{ctype.replace('_', '-')}-{short_hash(ctype, inputs)}", "type": ctype, "title": title,
        "check": ctype if ctype != "potential_tension" else "conditional_sign_tension",
        "framework": model.fw.ref, "inputs": inputs,
        "input_revisions": sorted({f"{l.claim['note']}@{l.claim['note_revision']}" for l in links}),
        "entities": sorted({e for l in links for e in (l.subj, l.obj)}),
        **fields,
    }


def _context_assumptions(model: ResearchModel, links: list[Link]) -> tuple[dict, list[str]]:
    cmp = model.context_compare([l.claim for l in links])
    out = []
    for f, values in cmp["mismatched"].items():
        out.append(f"The claims differ in {f} ({'; '.join(values)}); comparing them assumes the "
                   f"relationships carry over.")
    for f, ids in cmp["unknown"].items():
        out.append(f"{f} is not stated for {', '.join(ids)}; comparability is unknown.")
    return cmp, out


def _property_assumptions(links: list[Link], nodes: list[str]) -> list[str]:
    out = []
    for i, e in enumerate(nodes):
        props = set()
        for l in (links[i - 1], links[i]):
            p = l.claim["subject"] if l.subj == e else l.claim["object"]
            props.add(p.get("property", ""))
        if len(props) > 1:
            out.append(f"Treats {' and '.join(sorted(props))} of {e} as the same quantity (acting through "
                       f"its activity); check that link in the relevant setting.")
    for l in links:
        out += l.assumptions
    return list(dict.fromkeys(out))


def _shared_studies(links: list[Link]) -> list[str]:
    by_study = defaultdict(list)
    for l in links:
        by_study[l.study].append(l.id)
    return [f"{', '.join(ids)} come from the same study ({sid}); they are not independent."
            for sid, ids in by_study.items() if len(ids) > 1]


# ---- conditional sign tension ------------------------------------------------

def _signed_pairs(model: ResearchModel) -> dict[tuple[str, str], Link]:
    """One representative signed link per pair; pairs reported with both signs are left to
    conflicting_result."""
    out = {}
    for key, links in model.pairs.items():
        signed = [l for l in links if l.sign and l.kind in ("causal", "associative")]
        if not signed or len({l.sign for l in signed}) > 1:
            continue
        out[key] = sorted(signed, key=lambda l: (l.kind != "causal", -l.claim["extraction_confidence"], l.id))[0]
    return out


def _cycles(pairs: dict, max_len: int, cap: int = 5000) -> list[list[str]]:
    nbrs = defaultdict(list)
    for a, b in pairs:
        nbrs[a].append(b)
        nbrs[b].append(a)
    for v in nbrs:
        nbrs[v].sort()
    found: list[list[str]] = []

    def dfs(start, path):
        if len(found) >= cap:
            return
        for u in nbrs[path[-1]]:
            if u == start and len(path) >= 3 and path[1] < path[-1]:
                found.append(list(path))
            elif u > start and u not in path and len(path) < max_len:
                path.append(u)
                dfs(start, path)
                path.pop()

    for s in sorted(nbrs):
        dfs(s, [s])
    return found


def _feed_forward(links: list[Link], nodes: list[str]) -> tuple[str, str, list[str], list[str]] | None:
    """For an all-causal loop, find a source with two causal routes to a common sink."""
    n = len(nodes)
    for i, src in enumerate(nodes):
        out_left, out_right = links[i - 1], links[i]  # links touching nodes[i]
        if out_left.subj != src or out_right.subj != src:
            continue
        routes = []
        for step in (1, -1):
            path, j = [src], i
            while True:
                k = (j + step) % n
                link = links[j] if step == 1 else links[j - 1]
                if link.subj != nodes[j]:
                    break
                path.append(nodes[k])
                j = k
                nxt = links[j] if step == 1 else links[j - 1]
                if nxt.subj != nodes[j]:
                    break
            routes.append(path)
        if routes[0][-1] == routes[1][-1] and len(set(routes[0]) | set(routes[1])) == n:
            return src, routes[0][-1], routes[0], routes[1]
    return None


def _route_sign(model: ResearchModel, route: list[str]) -> int:
    sign = 1
    for u, v in zip(route, route[1:]):
        sign *= next(l.sign for l in model.links_between(u, v) if l.kind == "causal" and l.subj == u)
    return sign


def check_sign_tension(model: ResearchModel, max_loop: int = 4) -> list[dict]:
    pairs = _signed_pairs(model)
    cases = []
    for nodes in _cycles(pairs, max_loop):
        links = [pairs[tuple(sorted((nodes[i], nodes[(i + 1) % len(nodes)])))] for i in range(len(nodes))]
        product = 1
        for l in links:
            product *= l.sign
        if product > 0:
            continue
        # the challenging claim: the link spanning the widest scale distance (associations first);
        # the rest of the loop is the route it challenges
        def span(l):
            return abs(model.level(l.subj) - model.level(l.obj))
        challenger = max(links, key=lambda l: (span(l), l.kind == "associative", l.id))
        idx = links.index(challenger)
        a, b = nodes[idx], nodes[(idx + 1) % len(nodes)]
        low, high = (a, b) if model.level(a) <= model.level(b) else (b, a)
        ring = nodes[idx + 1:] + nodes[:idx + 1]  # starts at b, ends at a
        route = ring if ring[0] == low else ring[::-1]
        route_links = [l for l in links if l is not challenger]
        route_sign = 1
        for l in route_links:
            route_sign *= l.sign
        assoc = [l for l in route_links if l.kind == "associative"]
        all_causal = all(l.kind == "causal" for l in links)
        missing = []
        for l in assoc:
            x, y = sorted((l.subj, l.obj), key=model.level)
            missing.append(f"{x} → {y}: the notes report only an association ({l.id}). The route needs {x} "
                           f"to contribute to {y}; {x} might instead respond to {y} or share a cause with it.")
        cmp, ctx_assumptions = _context_assumptions(model, links)
        assumptions = ctx_assumptions + _property_assumptions(links, nodes) + _shared_studies(links)
        for l in links:
            assumptions += [f"{l.id}: {n}" for n in l.notes]
        ff = _feed_forward(links, nodes) if all_causal else None
        if ff:
            src, sink, r1, r2 = ff
            s1, s2 = _route_sign(model, r1), _route_sign(model, r2)
            explanations = [
                {"name": "Opposing routes coexist",
                 "premise": f"Both routes act; the net effect of {src} on {sink} depends on their balance "
                            "(an incoherent feed-forward arrangement), so neither claim need be wrong.",
                 "distinguishing": f"Block {r1[1]} and then {r2[1]} while changing {src}; measure {sink}. "
                                   "The net effect should strengthen when the opposing route is blocked."},
            ]
            if cmp["verdict"] != "compatible":
                explanations.append({
                    "name": "The routes operate in different settings",
                    "premise": "Each route was shown in a different setting (" +
                               ", ".join(list(cmp["mismatched"]) + [f"unknown {f}" for f in cmp["unknown"]]) +
                               "); in any one setting only one may act.",
                    "distinguishing": f"Test both routes in one matched setting, e.g. measure {r1[1]} and {r2[1]} "
                                      f"after changing {src} in the same cells or tissue."})
            explanations.append({
                "name": "One route does not hold as reported",
                "premise": "One of the causal claims is context-specific, or was mis-extracted.",
                "distinguishing": "Re-check the passages; replicate the weaker route."})
            cases.append(_case(
                "conditional_sign_tension", f"Opposing causal routes: {src} → {sink}", links, model,
                summary=(f"Two causal routes from {src} to {sink} have opposite net signs: "
                         f"{' → '.join(r1)} ({'+' if s1 > 0 else '−'}) and {' → '.join(r2)} "
                         f"({'+' if s2 > 0 else '−'})."),
                why=(f"A narrative choosing one route predicts the opposite effect of {src} on {sink} from "
                     "a narrative choosing the other."),
                route=r1, alt_route=r2, route_claims=[l.id for l in links], missing_premises=[],
                assumptions=list(dict.fromkeys(assumptions)), explanations=explanations,
                resolve=[e["distinguishing"] for e in explanations], context=cmp))
            continue
        route_txt = " → ".join(route)
        explanations = [{
            "name": f"The route {route_txt} holds",
            "premise": "Each step acts causally, in the stated direction, in the setting where the challenging "
                       "observation was made.",
            "distinguishing": f"Perturb {low} in a matched setting and measure {', '.join(route[1:-1]) or 'the route'} "
                              f"and a {high} readout: the route predicts {high} to {UP_DOWN[route_sign]} with "
                              f"{low}; the challenging claim predicts the opposite.",
        }]
        for l in assoc:
            x, y = sorted((l.subj, l.obj), key=model.level)
            explanations.append({
                "name": f"{x} responds to {y} rather than driving it",
                "premise": f"{y}, or a process upstream of it, induces {x}; {x} need not contribute to {y}.",
                "distinguishing": f"Establish temporal order of {x} and {y}; change {x} directly and test "
                                  f"whether {y} changes.",
            })
        if cmp["verdict"] != "compatible":
            explanations.append({
                "name": "Different contexts or routes",
                "premise": "The claims hold in different settings (" +
                           ", ".join(list(cmp["mismatched"]) + [f"unknown {f}" for f in cmp["unknown"]]) +
                           "), so they need not form one mechanism.",
                "distinguishing": f"Measure {low}, the intermediates and {high} in one matched setting; measure "
                                  f"{low}'s state directly where the challenging claim was observed.",
            })
        prop_issues = [x for x in assumptions if x.startswith(("Treats", "assumes"))]
        if prop_issues:
            explanations.append({
                "name": f"The {low} perturbation does not act as assumed",
                "premise": "The variant or state in one claim does not change activity the way another claim's "
                           "perturbation does (e.g. not loss-of-function, cell-type-specific, or dominant).",
                "distinguishing": f"Measure the effect of that state on {low} activity directly, in the relevant "
                                  "cells.",
            })
        if all_causal:
            explanations.append({
                "name": "Opposing routes coexist",
                "premise": "Both routes act; the net effect depends on their balance (an incoherent feed-forward "
                           "arrangement), so neither claim is wrong.",
                "distinguishing": "Block one route and test whether the net effect strengthens or flips.",
            })
        ctype = "conditional_sign_tension" if all_causal else "potential_tension"
        label = "Conditional sign tension" if all_causal else "Potential mechanistic tension"
        cases.append(_case(
            ctype, f"{label}: {', '.join(nodes)}", links, model,
            summary=(f"The route {route_txt} implies that {high} should {UP_DOWN[route_sign]} with {low}; "
                     f"{challenger.id} reports the opposite ({state_label(challenger.claim['subject'])} with "
                     f"{state_label(challenger.claim['object'])})."),
            why=(f"A linear narrative from {low} to {high} through {route_txt} cannot account for "
                 f"{challenger.id} without an additional premise."),
            challenger=challenger.id, route=route, route_claims=[l.id for l in route_links],
            missing_premises=missing, assumptions=list(dict.fromkeys(assumptions)), explanations=explanations,
            resolve=[e["distinguishing"] for e in explanations],
            context=cmp,
        ))
    return cases


# ---- conflicting results --------------------------------------------------------

def check_conflicts(model: ResearchModel) -> list[dict]:
    cases = []
    for (a, b), links in model.pairs.items():
        signed = [l for l in links if l.sign and l.kind in ("causal", "associative")]
        nulls = [l for l in links if l.kind == "null"]
        pos = [l for l in signed if l.sign > 0]
        neg = [l for l in signed if l.sign < 0]
        groups = []
        if pos and neg and {l.study for l in pos} != {l.study for l in neg}:
            groups.append(("opposite directions", pos + neg))
        if nulls and signed:
            groups.append(("an effect vs no effect", signed + nulls))
        for label, group in groups:
            cmp, ctx = _context_assumptions(model, group)
            designs = sorted({f"{l.claim['evidence']}" for l in group})
            explanations = [
                {"name": "Context dependence",
                 "premise": "The relationship differs between the settings studied.",
                 "distinguishing": "Repeat both measurements in one matched setting."},
                {"name": "Method or measurement differences",
                 "premise": "Assay, dose, timing or readout differ (" + ", ".join(designs) + " evidence).",
                 "distinguishing": "Compare the assays and time points; replicate with both methods."},
                {"name": "One result is unreliable",
                 "premise": "One study is underpowered, confounded, or its claim was mis-extracted.",
                 "distinguishing": "Check the passages and study design; seek an independent replication."},
            ]
            cases.append(_case(
                "conflicting_result", f"Conflicting results: {a} – {b} ({label})", group, model,
                summary=f"Studies disagree about {a} and {b}: {label}.",
                why="A narrative using this link must choose a side or state the condition under which each holds.",
                assumptions=ctx + _shared_studies(group), explanations=explanations,
                resolve=[e["distinguishing"] for e in explanations], context=cmp,
                sides={"positive": [l.id for l in pos], "negative": [l.id for l in neg],
                       "no_effect": [l.id for l in nulls]},
            ))
    return cases


# ---- independent evidence (positive triangulation) ------------------------------

def check_independence(model: ResearchModel, min_studies: int = 2, min_weight: float = 0.3) -> list[dict]:
    """Bare statements (evidence weight below `min_weight`) do not count as support."""
    cases = []
    for (a, b), links in model.pairs.items():
        signed = [l for l in links if l.sign and l.kind in ("causal", "associative")
                  and (model.fw.evidence_type(l.claim["evidence"]).weight
                       if model.fw.evidence_type(l.claim["evidence"]) else 0) >= min_weight]
        for sign in (1, -1):
            same = [l for l in signed if l.sign == sign]
            if not same:
                continue
            studies = defaultdict(list)
            for l in same:
                studies[l.study].append(l)
            if len(studies) < min_studies:
                continue
            target = sorted(same, key=lambda l: (l.kind != "causal", -l.claim["extraction_confidence"]))[0]
            others = [l for l in same if l.study != target.study]
            cmp, ctx = _context_assumptions(model, same)
            repeated = [f"{sid}: {', '.join(sorted({l.claim['note'] for l in ls}))}"
                        for sid, ls in studies.items() if len({l.claim['note'] for l in ls}) > 1]
            designs = sorted({l.kind for l in same})
            cases.append(_case(
                "triangulation", f"Independent support: {a} – {b}", same, model,
                summary=(f"{target.id} ({target.kind}) is supported by {len(studies) - 1} other independent "
                         f"stud{'y' if len(studies) == 2 else 'ies'}: {', '.join(sorted({l.id for l in others}))}."),
                why=("Independent studies agreeing on direction strengthen this link only if they measure the "
                     "same relationship in comparable settings."),
                strengthened=target.id, supporting=[l.id for l in others],
                independent_studies=sorted(studies), repeated_reports=repeated,
                designs=designs,
                assumptions=ctx + (["The supporting claims are associations; they corroborate direction, not "
                                    "causation."] if "associative" in designs and target.kind == "causal" else [])
                + [f"Study identity unknown for {l.id} ({l.claim['note']}); it may repeat another source."
                   for l in same if l.claim["study"].get("basis") == "unknown"],
                explanations=[], resolve=[], context=cmp,
            ))
    return cases


# ---- missing bridges ---------------------------------------------------------

def check_missing_bridges(model: ResearchModel) -> list[dict]:
    if not model.entities:
        return []
    top = max(model.level(e) for e in model.entities)
    cases = []
    for (a, b), links in model.pairs.items():
        ends = [e for e in (a, b) if model.level(e) == top]
        if not ends or any(l.kind == "causal" for l in links):
            continue
        assoc = [l for l in links if l.kind == "associative"]
        if not assoc:
            continue
        w = ends[0]
        x = b if w == a else a
        cases.append(_case(
            "missing_bridge", f"Missing causal bridge: {x} → {w}", assoc, model,
            summary=f"{x} and {w} are linked only by association ({', '.join(l.id for l in assoc)}).",
            why=f"Any narrative that passes from {x} to {w} rests on an assumed causal step.",
            assumptions=[], context=model.context_compare([l.claim for l in assoc]),
            explanations=[
                {"name": f"{x} contributes to {w}", "premise": f"Changing {x} changes {w}.",
                 "distinguishing": f"Perturb {x} in a model of {w} and measure {w} readouts."},
                {"name": f"{x} is a consequence of {w}", "premise": f"{w} induces {x}.",
                 "distinguishing": f"Establish whether {x} precedes {w}; induce {w} and measure {x}."},
                {"name": "Shared cause", "premise": f"A third process drives both {x} and {w}.",
                 "distinguishing": "Look for an upstream factor whose perturbation changes both."},
            ],
            resolve=[], missing_premises=[f"No causal evidence that {x} contributes to {w}."],
        ))
    return cases


def _group(cases: list[dict]) -> list[dict]:
    """Merge cases that raise the same issue (same type and title) so a reviewer sees it once."""
    groups: dict[tuple, list[dict]] = {}
    for c in cases:
        groups.setdefault((c["type"], c["title"]), []).append(c)
    out = []
    for (ctype, _), items in groups.items():
        if len(items) == 1:
            out.append(items[0])
            continue
        merged = dict(items[0])
        merged["inputs"] = sorted({i for c in items for i in c["inputs"]})
        merged["input_revisions"] = sorted({i for c in items for i in c["input_revisions"]})
        merged["entities"] = sorted({e for c in items for e in c["entities"]})
        merged["summary"] = " ".join(dict.fromkeys(c["summary"] for c in items))
        for key in ("route_claims", "assumptions", "missing_premises"):
            merged[key] = list(dict.fromkeys(x for c in items for x in c.get(key, [])))
        names = {}
        for c in items:
            for e in c.get("explanations", []):
                names.setdefault(e["name"], e)
        merged["explanations"] = list(names.values())
        merged["id"] = f"{ctype.replace('_', '-')}-{short_hash(ctype, merged['inputs'])}"
        out.append(merged)
    return out


def run_checks(model: ResearchModel) -> list[dict]:
    fw = model.fw
    cases = []
    if (c := fw.check("conditional_sign_tension")):
        cases += check_sign_tension(model, int(c.params.get("max_loop", 4)))
    if fw.check("conflicting_result"):
        cases += check_conflicts(model)
    if (c := fw.check("independent_evidence")):
        cases += check_independence(model, int(c.params.get("min_studies", 2)), float(c.params.get("min_weight", 0.3)))
    if fw.check("missing_bridge"):
        cases += check_missing_bridges(model)
    return _group(cases)
