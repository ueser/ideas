"""Render a framework lens as markdown under <output_dir>/<framework-id>/:

  index.md            scales, entity counts, best chains (Mermaid), consistency summary
  scales/<scale>.md   entities and notes at one scale
  entities/<name>.md  entity card: upstream, downstream, associations, evidence quotes, loops
  chains.md           ranked chains to the top-scale targets, with gaps and open points
  consistency.md      incoherent loops, conflicting links, best-triangulated links
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .mechanism import MechanismModel
from .vault import slugify

if TYPE_CHECKING:
    from .render import Renderer

ARROW = {"+": "↑", "-": "↓", "?": "?"}


def _mermaid(chain: dict) -> str:
    ids = {n: f"n{i}" for i, n in enumerate(chain["chain"])}
    lines = ["```mermaid", "flowchart LR"]
    for n, i in ids.items():
        step_scale = next((s["from_scale"] for s in chain["steps"] if s["from"] == n), None) or \
            next((s["to_scale"] for s in chain["steps"] if s["to"] == n), "")
        lines.append(f'  {i}["{n.replace(chr(34), chr(39))}<br/><small>{step_scale}</small>"]')
    for k, s in enumerate(chain["steps"]):
        arrow = "-->" if s["kind"] == "causal" else "-.->"
        lines.append(f'  {ids[s["from"]]} {arrow}|"{s["sign"]} {s["confidence"]:.2f}"| {ids[s["to"]]}')
    bad = [k for k, s in enumerate(chain["steps"]) if s["incoherent_loops"] or s["contested"]]
    if bad:
        lines.append(f"  linkStyle {','.join(map(str, bad))} stroke:#d33,stroke-width:2px")
    lines.append("```")
    return "\n".join(lines)


class MechanismRenderer:
    def __init__(self, r: Renderer, model: MechanismModel):
        self.r, self.m, self.fw = r, model, model.fw
        self.base = f"{r.cfg.output_dir}/{self.fw.id}"
        self.dir = r.cfg.root / self.base
        self.slugs: dict[str, str] = {}
        used: set[str] = set()
        for name in sorted(model.entities):
            s, n = slugify(name)[:60], 2
            while s in used:
                s, n = f"{slugify(name)[:60]}-{n}", n + 1
            used.add(s)
            self.slugs[name] = s

    def E(self, name: str, at) -> str:
        from .render import link
        return link(self.r.cfg, f"{self.base}/entities/{self.slugs[name]}", name, at)

    def P(self, page: str, title: str, at) -> str:
        from .render import link
        return link(self.r.cfg, f"{self.base}/{page}", title, at)

    def render(self) -> None:
        from .render import _header
        self.header = _header
        keep: dict[str, set[str]] = {"entities": set(), "scales": set()}
        for name in self.m.entities:
            self.render_entity(name)
            keep["entities"].add(self.slugs[name])
        for s in self.fw.scales:
            self.render_scale(s)
            keep["scales"].add(s.id)
        chains = {t: self.m.chains(t, k=3) for t in self.m.default_targets(5)}
        self.render_chains(chains)
        self.render_consistency()
        self.render_index(chains)
        for folder, names in keep.items():
            self.r.cleanup(self.dir / folder, names)

    def render_index(self, chains: dict) -> None:
        path = self.dir / "index.md"
        ov = self.m.overview()
        lines = [self.header(self.fw.name, "mechanism")]
        if self.fw.goal:
            lines.append(f"*{self.fw.goal}*\n")
        lines.append(f"{ov['claims']} claims · {ov['entities']} entities · {ov['links']} links · "
                     f"{ov['loops_checked']} loops checked, **{ov['incoherent_loops']} incoherent**\n")
        lines.append("## Scales\n")
        lines.append("| # | Scale | Entities | Most connected |")
        lines.append("|---|---|---|---|")
        for i, s in enumerate(ov["scales"]):
            top = ", ".join(self.E(n, path) for n in s["top"][:5])
            page = self.P("scales/" + s["scale"], s["name"], path)
            lines.append(f"| {i} | {page} | {s['entities']} | {top} |")
        best = [(t, c[0]) for t, c in chains.items() if c]
        if best:
            lines.append("\n## Best chains\n")
            for t, ch in best[:3]:
                lines.append(f"**→ {self.E(t, path)}** · covers {len(ch['scales_covered'])} scales\n")
                lines.append(_mermaid(ch) + "\n")
        lines.append(f"\nMore: {self.P('chains', 'All chains', path)} · "
                     f"{self.P('consistency', 'Consistency & triangulation', path)}")
        self.r._put(path, "\n".join(lines) + "\n")

    def render_scale(self, s) -> None:
        path = self.dir / "scales" / f"{s.id}.md"
        idx = self.fw.scale_index(s.id)
        lines = [self.header(f"{s.name} (scale {idx})", "mechanism-scale")]
        if s.description:
            lines.append(s.description + "\n")
        ents = sorted((e for e in self.m.entities.values() if e.scale == s.id),
                      key=lambda e: (-len(self.m.adj[e.name]), e.name))
        lines.append("## Entities\n")
        lines += [f"- {self.E(e.name, path)} — {len(self.m.adj[e.name])} links, {len(e.notes)} notes"
                  for e in ents] or ["- none yet"]
        notes = [r["note_id"] for r in self.r.store.db.execute(
            "SELECT note_id, scales FROM mech_extracted WHERE framework=?", (self.fw.id,))
                 if s.id in json.loads(r["scales"] or "[]")]
        lines.append("\n## Notes at this scale\n")
        lines += [f"- {self.r.L(n, path)}" for n in sorted(notes)] or ["- none yet"]
        nav = []
        if idx > 0:
            prev = self.fw.scales[idx - 1]
            nav.append(f"↓ {self.P('scales/' + prev.id, prev.name, path)}")
        if idx < len(self.fw.scales) - 1:
            nxt = self.fw.scales[idx + 1]
            nav.append(f"↑ {self.P('scales/' + nxt.id, nxt.name, path)}")
        nav.append(f"⌂ {self.P('index', self.fw.name, path)}")
        lines.append("\n" + " · ".join(nav))
        self.r._put(path, "\n".join(lines) + "\n")

    def render_entity(self, name: str) -> None:
        path = self.dir / "entities" / f"{self.slugs[name]}.md"
        rep = self.m.entity_report(name)
        scale = self.fw.scale(rep["scale"])
        lines = [self.header(name, "entity")]
        scale_link = self.P("scales/" + rep["scale"], scale.name if scale else rep["scale"], path)
        aliases = f" · also called: {', '.join(rep['aliases'])}" if rep["aliases"] else ""
        lines.append(f"Scale: {scale_link}{aliases}\n")

        def block(title: str, items: list, fmt) -> None:
            if not items:
                return
            lines.append(f"## {title}\n")
            for it in sorted(items, key=lambda x: -x["confidence"]):
                flag = " ⚠ conflicting" if it["contested"] else ""
                lines.append(f"- {fmt(it)} · confidence {it['confidence']:.2f}{flag}")
                for c in it["claims"][:3]:
                    q = f" — “{c['quote']}”" if c["quote"] else ""
                    ctx = f" [{c['context']}]" if c["context"] else ""
                    lines.append(f"  - {c['kind']}, {c['evidence']}{ctx}: {self.r.L(c['note'], path)}{q}")
            lines.append("")

        block("Upstream (causes)", rep["upstream"],
              lambda it: f"{self.E(it['entity'], path)} {ARROW[it['sign']] if it['sign'] != '?' else '→'} this "
                         f"(sign {it['sign']})")
        block("Downstream (effects)", rep["downstream"],
              lambda it: f"this → {self.E(it['entity'], path)} (sign {it['sign']})")
        block("Associations (correlative)", rep["associations"],
              lambda it: f"{self.E(it['entity'], path)} (sign {it['sign']})")
        if rep["incoherent_loops"]:
            lines.append("\n## Incoherent loops\n")
            for lp in rep["incoherent_loops"]:
                lines.append("- " + " — ".join(self.E(n, path) for n in lp["loop"]) +
                             f" · suspect link: {' – '.join(lp['suspect_link'])}")
        lines.append(f"\nCoherent loops through this entity: {rep['coherent_loops']}")
        lines.append("\n## Source notes\n")
        lines += [f"- {self.r.L(n, path)}" for n in rep["notes"]]
        self.r._put(path, "\n".join(lines) + "\n")

    def render_chains(self, chains: dict) -> None:
        path = self.dir / "chains.md"
        lines = [self.header("Mechanism chains", "chains")]
        lines.append("Chains climb the framework's scales and end at the most-connected entities at the top scale. "
                     "Solid arrows are causal, dashed are correlative; red marks links in incoherent loops or "
                     "with conflicting claims. Labels are sign and confidence.\n")
        for target, chs in chains.items():
            lines.append(f"## → {self.E(target, path)}\n")
            if not chs:
                lines.append("No chains yet.\n")
            for i, ch in enumerate(chs, 1):
                lines.append(f"### Chain {i} · score {ch['score']} · scales: {', '.join(ch['scales_covered'])}\n")
                lines.append(_mermaid(ch) + "\n")
                for s in ch["steps"]:
                    src = ", ".join(self.r.L(n, path) for n in s["sources"][:4])
                    lines.append(f"- {self.E(s['from'], path)} → {self.E(s['to'], path)} · {s['kind']}, sign "
                                 f"{s['sign']}, confidence {s['confidence']:.2f} · {src}")
                if ch["open_points"]:
                    lines.append("\n**Open points to test**\n")
                    lines += [f"- {p}" for p in ch["open_points"]]
                lines.append("")
        self.r._put(path, "\n".join(lines) + "\n")

    def render_consistency(self) -> None:
        path = self.dir / "consistency.md"
        rep = self.m.consistency(limit=100)
        lines = [self.header("Consistency & triangulation", "consistency")]
        lines.append(f"{rep['loops_checked']} loops checked: {rep['coherent']} coherent, "
                     f"**{rep['incoherent']} incoherent**.\n")
        lines.append("A loop is coherent when the signs of its links multiply to `+`: e.g. *A ↑ → X ↑*, "
                     "*X ↑ ~ W ↑* and *A ↓ ~ W ↑* multiply to `−`, so at least one of the three is wrong, "
                     "context-dependent, or missing a mediator.\n")
        lines.append("## Suspect links\n")
        lines.append("Links that sit in more incoherent than coherent loops: the likeliest places where a claim "
                     "is wrong, context-dependent, or hides a missing mediator.\n")
        lines += [f"- {self.E(x['between'][0], path)} – {self.E(x['between'][1], path)} · incoherent "
                  f"{x['incoherent_loops']}, coherent {x['coherent_loops']} · evidence {x['evidence']:.2f}"
                  + (f" · contexts: {'; '.join(x['contexts'])}" if x["contexts"] else "")
                  for x in rep["suspect_links"][:20]] or ["- none"]
        lines.append("\n## Incoherent loops\n")
        if not rep["incoherent_loops"]:
            lines.append("- none")
        for lp in rep["incoherent_loops"]:
            lines.append("- " + " — ".join(self.E(n, path) for n in lp["loop"]))
            for e in lp["edges"]:
                ctx = f" [{'; '.join(e['contexts'])}]" if e["contexts"] else ""
                src = ", ".join(self.r.L(n, path) for n in e["sources"][:3])
                weak = " ← suspect" if e["between"] == lp["suspect_link"] else ""
                lines.append(f"  - {e['between'][0]} {e['sign']} {e['between'][1]} · {'/'.join(e['kinds'])} · "
                             f"evidence {e['evidence']:.2f}{ctx} · {src}{weak}")
        lines.append("\n## Conflicting links\n")
        lines += [f"- {self.E(c['between'][0], path)} – {self.E(c['between'][1], path)} · "
                  f"+{c['support_plus']} / −{c['support_minus']} · "
                  + ", ".join(self.r.L(n, path) for n in c["sources"][:4]) for c in rep["contested_links"]] or ["- none"]
        lines.append("\n## Best triangulated links\n")
        lines += [f"- {self.E(c['between'][0], path)} – {self.E(c['between'][1], path)} · "
                  f"{c['coherent_loops']} coherent loops · confidence {c['confidence']:.2f}"
                  for c in rep["best_triangulated"][:30]] or ["- none"]
        self.r._put(path, "\n".join(lines) + "\n")
