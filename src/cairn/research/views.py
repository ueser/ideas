"""Markdown for reasoning records (cases, narratives, comparisons) and generated views
(index, framework map, entity cards, review queue). Records keep their review status and
reviewer notes across regeneration; views are pure projections and carry an input manifest."""

from __future__ import annotations

import os
import re

from ..config import Config
from ..vault import slugify
from .model import ResearchModel, state_label
from .records import Records, short_hash

STATUS_ICON = {"Supported": "●", "Assumed": "◐", "Challenged": "▲", "Missing": "○"}


class Linker:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def to(self, target_id: str, label: str, at, anchor: str = "") -> str:
        label = re.sub(r"[\[\]|]", "", label)
        if self.cfg.link_style == "markdown":
            rel = os.path.relpath(self.cfg.root / f"{target_id}.md", at.parent).replace(os.sep, "/")
            return f"[{label}]({rel.replace(' ', '%20')}{'#' + slugify(anchor) if anchor else ''})"
        return f"[[{target_id}{'#' + anchor if anchor else ''}|{label}]]"


def uncertainty(c: dict, model: ResearchModel | None = None) -> list[str]:
    flags = []
    if c.get("extraction_confidence", 1) < 0.6:
        flags.append(f"low extraction confidence ({c['extraction_confidence']})")
    m = c["span"].get("match", "exact")
    if m != "exact":
        flags.append("passage not found in note" if m == "not found" else f"passage matched {m}")
    for role in ("subject", "object"):
        if c[role].get("effect_basis") == "inferred":
            flags.append(f"{role} effect inferred")
    if not any(c["context"].values()):
        flags.append("no context stated")
    if c["study"].get("basis") == "unknown":
        flags.append("study identity unknown")
    if model is not None:
        link = next((l for l in model.links if l.id == c["id"]), None)
        if link:
            flags += link.notes
    return flags


def claim_line(L: Linker, c: dict, at, model: ResearchModel | None = None, show_flags: bool = True) -> str:
    neg = " (negated)" if c.get("negated") else ""
    cond = (" · if " + ", ".join(state_label(p) for p in c["conditions"])) if c.get("conditions") else ""
    ctx = "; ".join(f"{k}: {v}" for k, v in c["context"].items() if v)
    span = c["span"]
    where = L.to(c["note"], f"{c['note'].rsplit('/', 1)[-1]} L{span['line_start']}-{span['line_end']}", at,
                 span.get("heading", ""))
    flags = uncertainty(c, model) if show_flags else []
    flag_txt = f" · ⚠ {'; '.join(flags)}" if flags else ""
    return (f"`{c['id']}` {state_label(c['subject'])} —{c['predicate']}{neg}→ {state_label(c['object'])}{cond} · "
            f"{c['claim_type']}, {c['evidence']}{' · ' + ctx if ctx else ''} · study `{c['study']['id']}` · "
            f"*{c['status']}* · {where}{flag_txt}\n  > {span['text']}")


def _header(title: str, kind: str, inputs_rev: str = "") -> str:
    rev = f"\ninputs_revision: \"{inputs_rev}\"" if inputs_rev else ""
    return f"---\ntitle: \"{title}\"\ntype: {kind}\ngenerated_by: cairn{rev}\n---\n\n# {title}\n"


# ---- reasoning records ----------------------------------------------------------

def case_body(L: Linker, model: ResearchModel, case: dict, at) -> str:
    claims = model.claims
    out = [f"# {case['title']}\n", f"Check: `{case['check']}` · framework {case['framework']}\n", case["summary"], ""]
    out += ["## Why it matters\n", case["why"], ""]
    out.append("## Claims\n")
    order = ([case["challenger"]] if case.get("challenger") else []) + \
            [i for i in case.get("route_claims", []) if i != case.get("challenger")]
    order += [i for i in case["inputs"] if i not in order]
    for cid in order:
        role = ""
        if cid == case.get("challenger"):
            role = "**challenging claim** · "
        elif cid == case.get("strengthened"):
            role = "**strengthened claim** · "
        elif cid in case.get("route_claims", []):
            role = "route · "
        if cid in claims:
            out.append(f"- {role}{claim_line(L, claims[cid], at, model)}")
    if case.get("route"):
        out.append(f"\nRoute: {' → '.join(case['route'])}")
    if case.get("independent_studies"):
        out += ["", "## Independence\n",
                f"Independent studies: {', '.join(f'`{s}`' for s in case['independent_studies'])}"]
        if case.get("repeated_reports"):
            out.append("Repeated reports of one study (counted once): " + "; ".join(case["repeated_reports"]))
        out.append(f"Designs: {', '.join(case.get('designs', []))}")
    if case.get("sides"):
        out += ["", "## Sides\n"] + [f"- {k.replace('_', ' ')}: {', '.join(f'`{i}`' for i in v) or '-'}"
                                     for k, v in case["sides"].items()]
    if case.get("missing_premises"):
        out += ["", "## Missing premises\n"] + [f"- {m}" for m in case["missing_premises"]]
    if case.get("assumptions"):
        out += ["", "## Assumptions to check\n"] + [f"- {a}" for a in case["assumptions"]]
    if case.get("explanations"):
        out += ["", "## Competing explanations\n", "| Explanation | Additional premise | Distinguishing evidence |",
                "|---|---|---|"]
        for e in case["explanations"]:
            out.append(f"| {e['name']} | {e['premise']} | {e['distinguishing']} |")
        out.append("\n*Alternatives to investigate, not conclusions or protocols.*")
    if case.get("evidence_request"):
        out += ["", "## Evidence requested\n", case["evidence_request"]]
    return "\n".join(out)


def case_frontmatter(case: dict) -> dict:
    return {"id": case["id"], "type": "case", "case_type": case["type"], "title": case["title"],
            "check": case["check"], "framework": case["framework"], "status": case.get("status", "proposed"),
            "inputs": case["inputs"], "input_revisions": case["input_revisions"], "entities": case["entities"],
            "generated_by": "cairn"}


def narrative_body(L: Linker, model: ResearchModel, q: dict, label: str, n: dict, at) -> str:
    out = [f"# Narrative {label}: {n.get('title') or ' → '.join([n['steps'][0]['from']] + [s['to'] for s in n['steps']])}\n",
           f"> {q['question']}\n"]
    if n.get("summary"):
        out += [n["summary"], ""]
    out.append("Step status: ● Supported · ◐ Assumed · ▲ Challenged · ○ Missing\n")
    out += ["| # | Step | Status | How | Claims |", "|---|---|---|---|---|"]
    for i, s in enumerate(n["steps"], 1):
        out.append(f"| {i} | {s['from']} → {s['to']} | {STATUS_ICON[s['status']]} {s['status']} | "
                   f"{s.get('how', '')} | {', '.join(f'`{c}`' for c in s['claims']) or '—'} |")
    out.append("\n## Steps\n")
    for i, s in enumerate(n["steps"], 1):
        scales = f" ({s.get('from_scale', '?')} → {s.get('to_scale', '?')})" if s.get("from_scale") else ""
        out.append(f"### {i}. {s['from']} → {s['to']}{scales} · {s['status']}\n")
        if s.get("text"):
            out.append(s["text"] + "\n")
        for cid in s["claims"]:
            if cid in model.claims:
                out.append(f"- {claim_line(L, model.claims[cid], at, model)}")
        if s["status"] == "Missing":
            out.append("- No claim in the indexed notes connects these. This gap is part of the narrative.")
        for a in s.get("assumptions", []):
            out.append(f"- *assumption:* {a}")
        for cid in s.get("cases", []):
            out.append(f"- *challenged by* {L.to(f'{model.records.cfg.research_dir}/cases/{cid}', cid, at)}")
        out.append("")
    if n.get("net_sign"):
        src, tgt = n["steps"][0]["from"], n["steps"][-1]["to"]
        out += [f"Net direction: raising {src} should make {tgt} {'rise' if n['net_sign'] > 0 else 'fall'} "
                "if every step holds.\n"]
    if n.get("challenges"):
        out.append("## Challenges to this narrative\n")
        cases_dir = f"{model.records.cfg.research_dir}/cases"
        for c in n["challenges"]:
            out.append("- " + L.to(f"{cases_dir}/{c['id']}", c["title"], at))
        out.append("")
    cov = n.get("coverage")
    if cov:
        out += ["## Coverage for this question\n", f"Scales used: {', '.join(cov['scales_used'])}",
                f"Not used: {', '.join(cov['scales_not_used']) or '—'} — {cov['note']}", ""]
    if n.get("assumptions"):
        out += ["## Assumptions\n"] + [f"- {a}" for a in n["assumptions"]] + [""]
    if n.get("validation") is not None:
        out += ["## Validation\n"] + ([f"- {v}" for v in n["validation"]] or ["- all citations and statuses check out"])
    return "\n".join(out)


def comparison_body(L: Linker, q: dict, narr: list[dict], preds: list[dict], paths: list, at) -> str:
    out = ["# Competing narratives\n", f"> {q['question']}\n"]
    for label, p in zip("AB", paths):
        out.append(f"- {L.to(p, f'Narrative {label}', at)}")
    if len(narr) == 2:
        a_steps = {(s["from"], s["to"]) for s in narr[0]["steps"]}
        b_steps = {(s["from"], s["to"]) for s in narr[1]["steps"]}
        out += ["", "## Where they differ\n",
                "Only in A: " + ("; ".join(f"{u} → {v}" for u, v in sorted(a_steps - b_steps)) or "—"),
                "", "Only in B: " + ("; ".join(f"{u} → {v}" for u, v in sorted(b_steps - a_steps)) or "—")]
    else:
        out += ["", "Only one route exists in the evidence. Competing explanations of its links are listed in "
                    "the cases it cites."]
    if preds:
        out += ["", "## Distinguishing predictions\n", "| Observation | If A | If B | Falsifies |", "|---|---|---|---|"]
        for p in preds:
            out.append(f"| {p['observation']} | {p['if_A']} | {p['if_B']} | {p['falsifies']} |")
            if p.get("test"):
                out.append(f"| ↳ test | {p['test']} | | |")
        out.append("\n*Predictions are conditional on each narrative's premises; they are hypotheses to test.*")
    return "\n".join(out)


# ---- generated views ---------------------------------------------------------

class Views:
    def __init__(self, cfg: Config, recs: Records, model: ResearchModel):
        self.cfg, self.recs, self.m = cfg, recs, model
        self.L = Linker(cfg)
        self.base = f"{cfg.research_dir}/views"
        self.dir = cfg.research_path / "views"
        self.rev = short_hash(sorted((c["id"], c["status"]) for c in recs.all_claims()))
        self.changed: list[str] = []
        self.slugs = {e: slugify(e)[:60] for e in model.entities}

    def E(self, name: str, at) -> str:
        return self.L.to(f"{self.base}/entities/{self.slugs[name]}", name, at) if name in self.slugs else name

    def _put(self, path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
        path.write_text(text, encoding="utf-8")
        self.changed.append(path.relative_to(self.cfg.root).as_posix())

    def render(self) -> list[str]:
        for e in self.m.entities:
            self.entity(e)
        keep = {f"{s}.md" for s in self.slugs.values()}
        ent_dir = self.dir / "entities"
        if ent_dir.is_dir():
            for f in ent_dir.glob("*.md"):
                if f.name not in keep and "generated_by: cairn" in f.read_text(encoding="utf-8")[:200]:
                    f.unlink()
        self.framework_map()
        self.review_queue()
        self.index()
        return self.changed

    def index(self) -> None:
        path = self.dir / "index.md"
        fw = self.m.fw
        claims = self.recs.all_claims()
        counts = {}
        for c in claims:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        cases = self.recs.docs("cases")
        out = [_header(f"Research workspace: {fw.name}", "research-view", self.rev)]
        out.append(f"Framework {fw.ref} · {len(claims)} claims (" +
                   ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + f") · {len(self.m.entities)} entities\n")
        out.append(f"{self.L.to(f'{self.base}/framework-map', 'Framework map', path)} · "
                   f"{self.L.to(f'{self.base}/review-queue', 'Review queue', path)}\n")
        qs = [d for d in self.recs.docs("questions") if d["fm"].get("type") == "question"]
        out.append("## Questions\n")
        out += [f"- {self.L.to(d['path'].relative_to(self.cfg.root).as_posix()[:-3], d['fm']['question'], path)} "
                f"→ {d['fm'].get('target', '')}" for d in qs] or ["- none yet: `cairn research question new …`"]
        out.append("\n## Cases\n")
        by_type: dict[str, list] = {}
        for d in cases:
            by_type.setdefault(d["fm"].get("case_type", "case"), []).append(d)
        for t, ds in sorted(by_type.items()):
            out.append(f"### {t.replace('_', ' ')} ({len(ds)})\n")
            for d in ds:
                out.append(f"- {self.L.to(d['path'].relative_to(self.cfg.root).as_posix()[:-3], d['fm']['title'], path)}"
                           f" · *{d['fm'].get('status')}*")
            out.append("")
        self._put(path, "\n".join(out) + "\n")

    def framework_map(self) -> None:
        path = self.dir / "framework-map.md"
        fw = self.m.fw
        out = [_header(f"Framework map: {fw.name}", "research-view", self.rev)]
        out.append("Each lane is a scale. Entities appear in every scale their states are mapped to. "
                   "Counts are live claims (proposed + accepted).\n")
        lanes: dict[str, list[str]] = {s: [] for s in fw.scale_ids + ["unmapped"]}
        for e in self.m.entities:
            for s in self.m.entity_scales(e):
                lanes.setdefault(s, []).append(e)
        for s in fw.scales + [None]:
            sid = s.id if s else "unmapped"
            title = f"{fw.scale_index(sid)}. {s.name}" if s else "Unmapped"
            out.append(f"## {title}\n")
            ents = sorted(lanes.get(sid, []), key=lambda e: (-len(self.m.adj[e]), e))
            if not ents:
                out.append("*No supporting material found in the indexed scope.*\n")
                continue
            for e in ents:
                ups = sorted({l.other(e) for l in self.m.links if e in (l.subj, l.obj) and l.subj != l.obj})
                out.append(f"- {self.E(e, path)} · {len(self.m.entities[e]['claims'])} claims · "
                           f"{len(self.m.entities[e]['notes'])} notes · linked to {', '.join(ups[:6])}")
            out.append("")
        self._put(path, "\n".join(out) + "\n")

    def entity(self, name: str) -> None:
        path = self.dir / "entities" / f"{self.slugs[name]}.md"
        e = self.m.entities[name]
        out = [_header(name, "research-entity", self.rev)]
        maps = [m for m in self.m.mappings.values() if self.m.canonical(m["entity"]) == name]
        out.append("Scales: " + (", ".join(f"{m['property'] or '—'} → {'/'.join(m['scales']) or 'unmapped'} "
                                           f"(*{m['status']}*)" for m in maps) or "unmapped"))
        if e["aliases"]:
            out.append(f"\nAlso called: {', '.join(sorted(e['aliases']))}")
        groups: dict[str, list] = {}
        for l in self.m.links:
            if name in (l.subj, l.obj) and l.subj != l.obj:
                groups.setdefault(l.other(name), []).append(l)
        out.append("\n## Claims by connected entity\n")
        for other, links in sorted(groups.items()):
            studies = sorted({l.study for l in links})
            out.append(f"### {self.E(other, path)} · {len(links)} claims from {len(studies)} independent "
                       f"stud{'y' if len(studies) == 1 else 'ies'}\n")
            for l in links:
                out.append(f"- [{l.kind}] {claim_line(self.L, l.claim, path, self.m)}")
            out.append("")
        cases = [d for d in self.recs.docs("cases") if name in d["fm"].get("entities", [])]
        if cases:
            out.append("## Cases\n")
            out += [f"- {self.L.to(d['path'].relative_to(self.cfg.root).as_posix()[:-3], d['fm']['title'], path)} "
                    f"· *{d['fm'].get('status')}*" for d in cases]
        out.append("\n## Source notes\n")
        out += [f"- {self.L.to(n, n, path)}" for n in sorted(e["notes"])]
        self._put(path, "\n".join(out) + "\n")

    def review_queue(self) -> None:
        path = self.dir / "review-queue.md"
        q = review_queue(self.recs, self.m)
        out = [_header("Review queue", "research-view", self.rev)]
        out.append("Accept, revise, reject or defer with `cairn research review …`. Grouped by note; ⚠ marks "
                   "extractions worth checking first. Accepting records a review decision, not scientific truth.\n")
        out.append(f"## Claims ({sum(len(v) for v in q['claims'].values())})\n")
        for note, cs in q["claims"].items():
            out.append(f"### {self.L.to(note, note, path)} — `cairn research review accept --note {note}`\n")
            out += [f"- {claim_line(self.L, c, path, self.m)}" for c in cs]
            out.append("")
        for title, key in (("Cases", "cases"), ("Narratives", "narratives")):
            out.append(f"## {title} ({len(q[key])})\n")
            out += [f"- {self.L.to(d['path'].relative_to(self.cfg.root).as_posix()[:-3], d['fm'].get('title', d['fm']['id']), path)}"
                    f" · *{d['fm'].get('status')}*" + (f" — {d['fm']['status_reason']}" if d['fm'].get('status_reason') else "")
                    for d in q[key]] or ["- none"]
            out.append("")
        out.append(f"## Scale mappings ({len(q['mappings'])})\n")
        out += [f"- `map:{m['key']}` → {'/'.join(m['scales']) or 'unmapped'} ({m.get('votes', 0)} votes)"
                for m in q["mappings"][:200]] or ["- none"]
        out.append(f"\n## Identity merges ({len(q['entities'])})\n")
        out += [f"- `ent:{e['canonical']}` ← {', '.join(e['aliases'])}" for e in q["entities"]] or ["- none"]
        self._put(path, "\n".join(out) + "\n")


def review_queue(recs: Records, model: ResearchModel) -> dict:
    claims: dict[str, list] = {}
    for c in sorted(recs.all_claims(), key=lambda c: (c["note"], c["span"]["line_start"])):
        if c["status"] in ("proposed", "stale", "deferred"):
            claims.setdefault(c["note"], []).append(c)
    pending = ("proposed", "stale", "deferred")
    return {
        "claims": claims,
        "cases": [d for d in recs.docs("cases") if d["fm"].get("status") in pending],
        "narratives": [d for d in recs.docs("narratives") if d["fm"].get("status") in pending],
        "mappings": [m for m in model.mappings.values() if m.get("status") == "proposed"],
        "entities": [e for e in recs.entities() if e.get("status") == "proposed"],
    }
