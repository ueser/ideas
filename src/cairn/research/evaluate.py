"""Score a workspace against a gold set (claims in the extractor's output format plus the
findings an expert expects). Measures fidelity, not volume: more claims or cases is not better."""

from __future__ import annotations

import json
from pathlib import Path

from .model import effect, norm_name
from .records import DEAD


def _sign(fw, c: dict) -> int:
    es, _ = effect(c["subject"])
    eo, _ = effect(c["object"])
    if es and eo:
        return es * eo
    rel = fw.relation(c["predicate"])
    return (rel.sign if rel else 0) * (es or 1) * (eo or 1)


def _key(model, note: str, c: dict) -> tuple:
    return (note, norm_name(model.canonical(c["subject"]["entity"])), norm_name(model.canonical(c["object"]["entity"])),
            bool(c.get("negated")), _sign(model.fw, c))


def evaluate(ws, gold_dir: str) -> dict:
    gold_dir = Path(gold_dir)
    gold = json.loads((gold_dir / "claims.json").read_text())
    expected = json.loads((gold_dir / "expected.json").read_text())
    model = ws.model(statuses=("proposed", "accepted", "deferred"))
    records = [c for c in ws.recs.all_claims() if c["status"] not in DEAD]

    gold_keys = {}
    for note, g in gold.items():
        for c in g["claims"]:
            gold_keys.setdefault(_key(model, note, c), c)
    rec_keys = {}
    for c in records:
        rec_keys.setdefault(_key(model, c["note"], c), c)
    matched = set(gold_keys) & set(rec_keys)
    ctx_right = ctx_wrong = ctx_invented = ctx_missed = 0
    type_agree = 0
    for k in matched:
        g, r = gold_keys[k]["context"], rec_keys[k]["context"]
        for f, gv in g.items():
            rv = r.get(f)
            if gv is None and rv:
                ctx_invented += 1
            elif gv and not rv:
                ctx_missed += 1
            elif gv and rv:
                if str(gv).lower() in str(rv).lower() or str(rv).lower() in str(gv).lower():
                    ctx_right += 1
                else:
                    ctx_wrong += 1
        type_agree += gold_keys[k]["claim_type"] == rec_keys[k]["claim_type"]
    exact = sum(1 for c in records if c["span"].get("match") == "exact")

    cases = ws.recs.docs("cases")
    found_cases = []
    for exp in expected.get("cases", []):
        want = {norm_name(model.canonical(e)) for e in exp["entities"]}
        hit = next((d["fm"]["id"] for d in cases if d["fm"].get("case_type") == exp["type"]
                    and want <= {norm_name(e) for e in d["fm"].get("entities", [])}), None)
        found_cases.append({**exp, "found": hit})

    single = []
    for a, b in expected.get("single_study_pairs", []):
        ea, eb = model.find_entity(a), model.find_entity(b)
        links = model.links_between(ea, eb) if ea and eb else []
        single.append({"pair": [a, b], "notes": len({l.claim["note"] for l in links}),
                       "studies": len({l.study for l in links}),
                       "ok": bool(links) and len({l.study for l in links}) == 1})
    negated = {n: any(c["negated"] for c in records if c["note"] == n) for n in expected.get("negated_claims_in", [])}
    unknown_ctx = {n: all(not any(c["context"].values()) for c in records if c["note"] == n)
                   for n in expected.get("unknown_context_notes", [])}

    per_note = {}
    for note in sorted({k[0] for k in gold_keys} | {k[0] for k in rec_keys}):
        miss = [gold_keys[k]["quote"] for k in gold_keys if k[0] == note and k not in matched]
        extra = [rec_keys[k]["span"]["text"] for k in rec_keys if k[0] == note and k not in matched]
        if miss or extra:
            per_note[note] = {"missed": miss, "extra": extra}
    n_ctx = ctx_right + ctx_wrong
    return {
        "claims": {"gold": len(gold_keys), "extracted": len(rec_keys), "matched": len(matched),
                   "precision": round(len(matched) / len(rec_keys), 3) if rec_keys else 0.0,
                   "recall": round(len(matched) / len(gold_keys), 3) if gold_keys else 0.0,
                   "claim_type_agreement": round(type_agree / len(matched), 3) if matched else 0.0},
        "context": {"correct": ctx_right, "wrong": ctx_wrong, "invented": ctx_invented, "missed": ctx_missed,
                    "accuracy": round(ctx_right / n_ctx, 3) if n_ctx else 0.0},
        "passages": {"exact": exact, "total": len(records),
                     "not_found": sum(1 for c in records if c["span"].get("match") == "not found")},
        "cases": found_cases,
        "independence": single,
        "negation_captured": negated,
        "unknown_context_kept_unknown": unknown_ctx,
        "per_note": per_note,
    }


def format_report(r: dict) -> str:
    c, x, p = r["claims"], r["context"], r["passages"]
    lines = [
        f"Claims     precision {c['precision']:.2f} · recall {c['recall']:.2f} "
        f"({c['matched']} matched / {c['extracted']} extracted / {c['gold']} gold) · "
        f"claim-type agreement {c['claim_type_agreement']:.2f}",
        f"Context    accuracy {x['accuracy']:.2f} · wrong {x['wrong']} · invented {x['invented']} · missed {x['missed']}",
        f"Passages   {p['exact']}/{p['total']} anchored exactly · {p['not_found']} not found",
        "Cases",
    ]
    for case in r["cases"]:
        lines.append(f"  {'✓' if case['found'] else '✗'} {case['type']}: {', '.join(case['entities'])}"
                     + (f"  ({case['found']})" if case["found"] else ""))
    for s in r["independence"]:
        lines.append(f"Independence  {'✓' if s['ok'] else '✗'} {' – '.join(s['pair'])}: {s['notes']} notes, "
                     f"{s['studies']} stud{'y' if s['studies'] == 1 else 'ies'}")
    for n, ok in r["negation_captured"].items():
        lines.append(f"Negation      {'✓' if ok else '✗'} {n}")
    for n, ok in r["unknown_context_kept_unknown"].items():
        lines.append(f"Unknown ctx   {'✓' if ok else '✗'} {n}")
    for note, d in r["per_note"].items():
        lines.append(f"  {note}: missed {len(d['missed'])}, extra {len(d['extra'])}")
    return "\n".join(lines)
