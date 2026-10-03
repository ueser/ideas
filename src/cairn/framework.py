"""User-supplied analytical frameworks ("lenses").

A framework tells cairn how to read the notes for one purpose: an ordered set
of scales, the kinds of evidence that count (and how much), and free-text
guidance that is handed to Claude verbatim. Frameworks are TOML files; see
examples/frameworks/disease-mechanism.toml.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .vault import slugify

DEFAULT_EVIDENCE = [
    {"id": "intervention", "weight": 1.0, "causal": True,
     "description": "perturbation experiment: knockout, knockdown, overexpression, inhibitor, CRISPR, rescue"},
    {"id": "genetic", "weight": 0.8, "causal": True,
     "description": "genetic evidence: patient variants, Mendelian randomization, model-organism mutants"},
    {"id": "observational", "weight": 0.4, "causal": False,
     "description": "correlation or co-occurrence in samples, patients or cohorts"},
    {"id": "computational", "weight": 0.3, "causal": False,
     "description": "prediction, modelling, network inference"},
    {"id": "stated", "weight": 0.25, "causal": False,
     "description": "asserted without evidence shown in the note (e.g. background statement, review)"},
]


@dataclass
class Scale:
    id: str
    name: str
    description: str = ""


@dataclass
class EvidenceType:
    id: str
    weight: float
    causal: bool
    description: str = ""


@dataclass
class Framework:
    id: str
    name: str
    goal: str
    guidance: str
    scales: list[Scale]
    evidence: list[EvidenceType] = field(default_factory=list)
    path: Path | None = None

    @property
    def hash(self) -> str:
        """Changes when anything that affects extraction changes, so claims get re-extracted."""
        data = {k: v for k, v in asdict(self).items() if k != "path"}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]

    @property
    def scale_ids(self) -> list[str]:
        return [s.id for s in self.scales]

    def scale_index(self, scale_id: str | None) -> int:
        try:
            return self.scale_ids.index(scale_id)
        except ValueError:
            return -1

    def scale(self, scale_id: str) -> Scale | None:
        return next((s for s in self.scales if s.id == scale_id), None)

    def evidence_weight(self, evidence_id: str) -> float:
        return next((e.weight for e in self.evidence if e.id == evidence_id), 0.25)

    def describe(self) -> str:
        """Plain-text rendering used in prompts and `cairn mech framework`."""
        lines = [f"Framework: {self.name}", f"Goal: {self.goal}", "", "Scales, lowest to highest:"]
        lines += [f"  {i}. {s.id} — {s.name}" + (f": {s.description}" if s.description else "")
                  for i, s in enumerate(self.scales)]
        lines += ["", "Evidence types (weight):"]
        lines += [f"  - {e.id} ({e.weight}{', causal' if e.causal else ''}): {e.description}" for e in self.evidence]
        if self.guidance.strip():
            lines += ["", "Guidance:", self.guidance.strip()]
        return "\n".join(lines)


def load_framework_file(path: Path) -> Framework:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    scales = [Scale(id=slugify(s["id"]).replace("-", "_"), name=s.get("name", s["id"]),
                    description=s.get("description", "")) for s in data.get("scales", [])]
    if len(scales) < 2:
        raise SystemExit(f"cairn: framework {path} needs at least two [[scales]]")
    evidence = [EvidenceType(id=e["id"], weight=float(e.get("weight", 0.5)), causal=bool(e.get("causal", False)),
                             description=e.get("description", "")) for e in data.get("evidence", DEFAULT_EVIDENCE)]
    name = data.get("name", path.stem)
    return Framework(id=slugify(data.get("id", name)), name=name, goal=data.get("goal", ""),
                     guidance=data.get("guidance", ""), scales=scales, evidence=evidence, path=path)
