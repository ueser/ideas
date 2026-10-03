"""`cairn research …` commands."""

from __future__ import annotations

import argparse
import json
import sys

from ..tools import VaultTools
from .model import state_label


def _ctx(args):
    from ..cli import _fresh, _open
    from .workspace import Workspace
    cfg, store = _open(args)
    _fresh(cfg, store)
    return cfg, store, Workspace(cfg, store)


def _llm(cfg):
    from ..agent import Claude
    return Claude(cfg)


def _out(obj, args, human):
    if getattr(args, "json", False):
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        human(obj)


def _err(msg):
    print(msg, file=sys.stderr)


def _progress(msg):
    _err(f"  · {msg[:160]}")


def cmd_framework(args):
    from ..config import load_config
    cfg = load_config(args.vault)
    fw = cfg.load_framework()
    if fw is None:
        raise SystemExit("cairn: no framework configured")
    if args.impact:
        from .workspace import Workspace
        _out(Workspace(cfg).framework_impact(), args, lambda r: print(json.dumps(r, indent=2)))
    else:
        print(fw.describe())
        if fw.guide:
            print(f"\nGuide: {fw.path.parent / 'guide.md'} ({len(fw.guide.splitlines())} lines)")


def cmd_sync(args):
    _, _, ws = _ctx(args)
    rep = ws.sync()
    _out(rep, args, lambda r: print(", ".join(f"{k}: {len(v)}" for k, v in r.items())))


def cmd_extract(args):
    from .extract import Extractor
    cfg, store, ws = _ctx(args)
    ws.sync()
    ex = Extractor(cfg, store, _llm(cfg), ws.fw)
    n = ex.extract(force=args.force, limit=args.limit, notes=args.note or None)
    m = ex.normalize()
    rep = ws.sync()
    ws.render()
    print(f"proposed {n} claim(s); {m} identity merge(s) proposed; {len(rep['stale_docs'])} record(s) now stale")


def cmd_check(args):
    _, _, ws = _ctx(args)
    ws.sync()
    rep = ws.check()
    ws.render()
    print(f"cases: {rep['created']} new, {rep['updated']} updated, {rep['unchanged']} unchanged, "
          f"{rep['removed']} withdrawn, {rep['staled']} stale · " +
          ", ".join(f"{k}: {v}" for k, v in sorted(rep["by_type"].items())))


def cmd_run(args):
    cfg, store, ws = _ctx(args)
    ws.sync()
    if not args.no_llm:
        from .extract import Extractor
        ex = Extractor(cfg, store, _llm(cfg), ws.fw)
        _err(f"proposed {ex.extract(limit=args.limit)} claim(s)")
        _err(f"proposed {ex.normalize()} identity merge(s)")
        ws.sync()
    ws.check()
    ws.render()
    q = ws.queue()
    print(f"{sum(len(v) for v in q['claims'].values())} claims and {len(q['cases'])} cases await review → "
          f"{cfg.research_dir}/views/review-queue.md")


def cmd_map(args):
    _, _, ws = _ctx(args)
    m = ws.model()
    lanes = {}
    for e in m.entities:
        for s in m.entity_scales(e):
            lanes.setdefault(s, []).append(e)
    res = {s: sorted(lanes.get(s, [])) for s in ws.fw.scale_ids + ["unmapped"]}

    def human(r):
        for i, (s, ents) in enumerate(r.items()):
            label = f"{i}. {s}" if s != "unmapped" else "   unmapped"
            print(f"{label:<22} " + (", ".join(ents) if ents else "— no supporting material in the indexed scope"))
    _out(res, args, human)


def cmd_claims(args):
    cfg, store, _ = _ctx(args)
    res = VaultTools(cfg, store).find_claims(entity=args.entity, note=args.note, status=args.status,
                                             text=args.text, limit=args.limit)
    _out(res, args, lambda rs: [print(f"{c['id']}  [{c['status']}]  {c['claim']}  · {c['type']}, {c['evidence']} "
                                      f"· {c['note']} L{c['lines']}") for c in rs] or print("no claims"))


def cmd_claim(args):
    cfg, store, _ = _ctx(args)
    c = VaultTools(cfg, store).get_claim(args.id)

    def human(c):
        print(f"{c['id']}  [{c['status']}{' — ' + c['status_reason'] if c.get('status_reason') else ''}]")
        print(f"  {state_label(c['subject'])} —{c['predicate']}{' (negated)' if c['negated'] else ''}→ "
              f"{state_label(c['object'])}")
        if c["conditions"]:
            print("  only when: " + ", ".join(state_label(p) for p in c["conditions"]))
        print(f"  {c['claim_type']} · {c['evidence']} evidence · read as {c['read_as']} "
              f"(sign {c['sign']:+d})" if c['sign'] is not None else "")
        print(f"  attribution: {c['attribution']} · study: {c['study']['id']} ({c['study']['basis']})")
        ctx = {k: v for k, v in c["context"].items() if v}
        print("  context: " + (", ".join(f"{k}={v}" for k, v in ctx.items()) or "none stated"))
        for n in c["model_notes"]:
            print(f"  ! {n}")
        print(f"\n  source: {c['note']}  lines {c['span']['line_start']}-{c['span']['line_end']} "
              f"(§ {c['span']['heading'] or '—'}, match: {c['span'].get('match')}, revision {c['note_revision']})")
        print("  passage: “" + c["span"]["text"] + "”")
        if c["source_excerpt"]:
            print("\n" + "\n".join("  " + line for line in c["source_excerpt"].splitlines()))
        for r in c["related_claims"]:
            print(f"  ↔ {r['id']} {r['relation']} (study {r['study']}, {r['status']})")
        for r in c["reviews"]:
            print(f"  ✎ {r['at']} {r['action']} {r['rationale']}")
    _out(c, args, human)


def cmd_entity(args):
    cfg, store, ws = _ctx(args)
    m = ws.model()
    name = m.find_entity(" ".join(args.name))
    if name is None:
        raise SystemExit(f"cairn: no entity '{' '.join(args.name)}'")
    e = m.entities[name]
    groups = {}
    for l in m.links:
        if name in (l.subj, l.obj) and l.subj != l.obj:
            groups.setdefault(l.other(name), []).append(l)
    res = {"name": name, "scales": m.entity_scales(name), "states": dict(e["states"]),
           "aliases": sorted(e["aliases"]), "notes": sorted(e["notes"]),
           "connections": {o: [{"id": l.id, "kind": l.kind, "sign": l.sign, "study": l.study,
                                "status": l.claim["status"]} for l in ls] for o, ls in sorted(groups.items())}}

    def human(r):
        print(f"{r['name']}  scales: {', '.join(r['scales'])}  states: {', '.join(r['states'])}")
        for other, ls in r["connections"].items():
            studies = len({x["study"] for x in ls})
            print(f"  ↔ {other}: " + ", ".join(f"{x['id']} {x['kind']}{x['sign']:+d}" for x in ls) +
                  f"  ({studies} independent stud{'y' if studies == 1 else 'ies'})")
    _out(res, args, human)


def cmd_cases(args):
    cfg, store, _ = _ctx(args)
    res = VaultTools(cfg, store).list_cases(args.type, args.status)
    _out(res, args, lambda rs: [print(f"{c['id']:<34} [{c['status']}] {c['title']}") for c in rs] or print("no cases"))


def cmd_case(args):
    cfg, store, _ = _ctx(args)
    res = VaultTools(cfg, store).get_case(args.id)
    _out(res, args, lambda r: print(r["text"]))


def cmd_question(args):
    from .narratives import create_question
    cfg, store, ws = _ctx(args)
    if args.action == "new":
        m = ws.model()
        if m.find_entity(args.target) is None:
            _err(f"note: '{args.target}' is not (yet) an entity in the claims; narratives need it to exist")
        fm = create_question(ws.recs, ws.fw, " ".join(args.text), args.target, args.source)
        ws.render()
        print(f"{fm['id']}  → {cfg.research_dir}/questions/{fm['id']}.md")
    else:
        for d in ws.recs.docs("questions"):
            if d["fm"].get("type") == "question":
                print(f"{d['fm']['id']:<40} {d['fm']['question']}  → {d['fm'].get('target')}")


def cmd_path(args):
    cfg, store, _ = _ctx(args)
    res = VaultTools(cfg, store).inspect_path(args.to, args.source)

    def human(paths):
        if not paths:
            print("no path")
        icon = {"Supported": "●", "Assumed": "◐", "Challenged": "▲", "Missing": "○"}
        for i, p in enumerate(paths, 1):
            print(f"Path {i}")
            for s in p["steps"]:
                print(f"  {icon[s['status']]} {s['from']} → {s['to']}  [{s['status']}; {s['how']}] "
                      f"{', '.join(s['claims'])}")
            print(f"  scales not used (may be irrelevant): {', '.join(p['coverage']['scales_not_used']) or '—'}\n")
    _out(res, args, human)


def cmd_context(args):
    cfg, store, _ = _ctx(args)
    pkg = VaultTools(cfg, store).compose_context(args.question)

    def human(p):
        print(f"Question: {p['question']}  ({p['framework']}, records {p['records_revision']}, "
              f"~{p['token_estimate']} tokens)")
        for n in p["narratives"]:
            print(f"  Narrative {n['label']}: " + " → ".join([n['steps'][0]['from']] + [s['to'] for s in n['steps']]))
        print(f"  claims: {len(p['claims'])} · counterevidence: {len(p['counterevidence'])} · cases: "
              f"{len(p['cases'])} · gaps: {len(p['gaps'])} · assumptions: {len(p['assumptions'])}")
        ex = p["exclusions"]
        print(f"  excluded: {len(ex['claims_not_live'])} non-live claims, {len(ex['unmapped_entities'])} unmapped "
              "entities")
    _out(pkg, args, human)


def cmd_narrate(args):
    cfg, store, ws = _ctx(args)
    ws.sync()
    if not ws.recs.docs("cases"):
        ws.check()
    written = ws.narrate(args.question, llm=None if args.no_llm else _llm(cfg))
    ws.render()
    for w in written:
        print(w)


def cmd_investigate(args):
    cfg, store, ws = _ctx(args)
    ws.sync()
    report, written = ws.investigate(args.question, _llm(cfg), max_turns=args.turns,
                                     on_event=None if args.quiet else _progress)
    ws.render()
    print(report)
    for w in written:
        _err(f"wrote {w}")


def cmd_review(args):
    cfg, store, ws = _ctx(args)
    if args.action == "queue":
        q = ws.queue()
        res = {"claims": {n: [c["id"] for c in cs] for n, cs in q["claims"].items()},
               "cases": [d["fm"]["id"] for d in q["cases"]], "narratives": [d["fm"]["id"] for d in q["narratives"]],
               "mappings": [m["key"] for m in q["mappings"]], "entities": [e["canonical"] for e in q["entities"]]}

        def human(r):
            from .views import uncertainty
            for note, ids in r["claims"].items():
                print(f"{note}  ({len(ids)} claims)  → cairn research review accept --note {note}")
                for cid in ids:
                    c = ws.recs.claim(cid)
                    flags = uncertainty(c)
                    print(f"   {cid} [{c['status']}] {state_label(c['subject'])} —{c['predicate']}→ "
                          f"{state_label(c['object'])}" + (f"   ⚠ {'; '.join(flags)}" if flags else ""))
            for key in ("cases", "narratives"):
                for i in r[key]:
                    print(f"{key[:-1]}: {i}")
            print(f"mappings: {len(r['mappings'])} · identity merges: {len(r['entities'])}")
        _out(res, args, human)
        return
    done = ws.review(args.action, args.ids, note=args.note, rationale=args.message or "", sets=args.set)
    ws.render()
    print(f"{args.action}: {', '.join(done) or 'nothing'}")


def cmd_views(args):
    _, _, ws = _ctx(args)
    for p in ws.render():
        print(p)


def cmd_eval(args):
    from .evaluate import evaluate, format_report
    cfg, store, ws = _ctx(args)
    rep = evaluate(ws, args.gold)
    _out(rep, args, lambda r: print(format_report(r)))


def add_parser(sub) -> None:
    rp = sub.add_parser("research", help="research workspace: evidence-linked claims, cases, narratives, review",
                        description="Evidence-linked claims, framework mappings, reasoning cases, competing "
                                    "narratives and review, stored under the research folder.")
    rs = rp.add_subparsers(dest="research_cmd", required=True)

    def add(name, fn, help_):
        sp = rs.add_parser(name, help=help_, description=help_)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    add("framework", cmd_framework, "show the framework, or --impact of a version change").add_argument(
        "--impact", action="store_true")
    add("sync", cmd_sync, "follow renames/edits; mark dependent claims, cases and narratives stale")
    sp = add("extract", cmd_extract, "LLM: propose claims for new/changed notes, then identity merges")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--note", action="append", help="only this note id (repeatable)")
    add("check", cmd_check, "run the framework's checks; write cases for review")
    sp = add("run", cmd_run, "sync → extract → normalize → check → views")
    sp.add_argument("--no-llm", action="store_true")
    sp.add_argument("--limit", type=int)
    add("map", cmd_map, "framework map: entities per scale lane")
    sp = add("claims", cmd_claims, "list claims")
    for a in ("--entity", "--note", "--status", "--text"):
        sp.add_argument(a)
    sp.add_argument("-n", "--limit", type=int, default=50)
    add("claim", cmd_claim, "evidence inspector: a claim, its passage in context, related claims, reviews"
        ).add_argument("id")
    add("entity", cmd_entity, "an entity's states, scales and claims by connected entity").add_argument("name", nargs="+")
    sp = add("cases", cmd_cases, "list reasoning cases")
    sp.add_argument("--type")
    sp.add_argument("--status")
    add("case", cmd_case, "show a case").add_argument("id")
    sp = add("question", cmd_question, "create or list research questions")
    sp.add_argument("action", choices=["new", "list"])
    sp.add_argument("text", nargs="*")
    sp.add_argument("--target", help="phenotype / entity the question is about")
    sp.add_argument("--source", help="starting perturbation (optional)")
    sp = add("path", cmd_path, "candidate reading paths with step status")
    sp.add_argument("--to", required=True)
    sp.add_argument("--from", dest="source")
    add("context", cmd_context, "the evidence package an agent would receive for a question").add_argument("question")
    sp = add("narrate", cmd_narrate, "competing narratives + distinguishing predictions for a question")
    sp.add_argument("question")
    sp.add_argument("--no-llm", action="store_true", help="deterministic skeletons only")
    sp = add("investigate", cmd_investigate, "LLM agent: bounded investigation of a question; proposes cases")
    sp.add_argument("question")
    sp.add_argument("--turns", type=int, default=30)
    sp.add_argument("-q", "--quiet", action="store_true")
    sp = add("review", cmd_review, "review queue, or accept / reject / defer / revise proposals")
    sp.add_argument("action", choices=["queue", "accept", "reject", "defer", "revise"])
    sp.add_argument("ids", nargs="*", help="claim ids, case/narrative ids, map:<entity|property>, ent:<name>")
    sp.add_argument("--note", help="apply to all proposed claims from this note")
    sp.add_argument("-m", "--message", help="rationale")
    sp.add_argument("--set", action="append", help="revise: field=value (dot paths, JSON values)")
    add("views", cmd_views, "regenerate the research views")
    add("eval", cmd_eval, "score extraction and checks against a gold set").add_argument("--gold", required=True)
