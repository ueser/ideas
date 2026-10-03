"""Versioned frameworks: an analysis contract with a readable guide and a typed definition.

The typed definition (TOML) declares scales, relation types (with kind and sign), the
context fields every claim should carry, which fields must match before two claims are
compared, evidence types and which of them can justify a causal reading, the questions the
framework is for, and the checks that are eligible to run. The guide (Markdown) explains
concepts and examples; it is handed to the extractor but never executed as rules.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

RELATION_KINDS = ("causal", "associative", "compositional", "descriptive")
KNOWN_CHECKS = ("conditional_sign_tension", "conflicting_result", "independent_evidence", "missing_bridge")

DEFAULT_RELATIONS = [
    {"id": "increases", "kind": "causal", "sign": 1, "description": "changing the subject state moves the object the same way"},
    {"id": "decreases", "kind": "causal", "sign": -1, "description": "changing the subject state moves the object the opposite way"},
    {"id": "contributes_to", "kind": "causal", "sign": 1, "description": "the subject is a cause of (part of the cause of) the object"},
    {"id": "associated_with", "kind": "associative", "sign": 0, "description": "co-vary; the sign comes from the directions"},
    {"id": "positively_associated", "kind": "associative", "sign": 1, "description": "the two co-vary in the same direction"},
    {"id": "negatively_associated", "kind": "associative", "sign": -1, "description": "the two co-vary in opposite directions"},
    {"id": "observed_in", "kind": "associative", "sign": 1, "description": "the subject state is found in / elevated in the object (e.g. a condition)"},
    {"id": "encodes", "kind": "compositional", "sign": 0, "description": "gene encodes product"},
    {"id": "part_of", "kind": "compositional", "sign": 0, "description": "subject is a component of object"},
    {"id": "binds", "kind": "descriptive", "sign": 0, "description": "physical interaction"},
]

DEFAULT_EVIDENCE = [
    {"id": "intervention", "weight": 1.0, "supports_causal": True, "description": "perturbation experiment"},
    {"id": "genetic", "weight": 0.8, "supports_causal": True, "description": "genetic evidence"},
    {"id": "observational", "weight": 0.4, "supports_causal": False, "description": "correlation in samples or cohorts"},
    {"id": "computational", "weight": 0.3, "supports_causal": False, "description": "prediction or modelling"},
    {"id": "stated", "weight": 0.2, "supports_causal": False, "description": "asserted without data in the note"},
]


@dataclass
class Scale:
    id: str
    name: str
    description: str = ""


@dataclass
class Relation:
    id: str
    kind: str
    sign: int
    description: str = ""


@dataclass
class EvidenceType:
    id: str
    weight: float
    supports_causal: bool
    description: str = ""


@dataclass
class Check:
    id: str
    enabled: bool = True
    params: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class Framework:
    id: str
    version: int
    name: str
    purpose: str
    scales: list[Scale]
    relations: list[Relation]
    context_fields: list[str]
    context_match: list[str]
    evidence: list[EvidenceType]
    questions: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    guide: str = ""
    path: Path | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def content_hash(self) -> str:
        """Detects edits made without bumping `version`."""
        data = {k: v for k, v in asdict(self).items() if k != "path"}
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]

    @property
    def scale_ids(self) -> list[str]:
        return [s.id for s in self.scales]

    def scale_index(self, scale_id: str | None) -> int:
        return self.scale_ids.index(scale_id) if scale_id in self.scale_ids else -1

    def scale(self, scale_id: str) -> Scale | None:
        return next((s for s in self.scales if s.id == scale_id), None)

    def relation(self, rel_id: str) -> Relation | None:
        return next((r for r in self.relations if r.id == rel_id), None)

    def evidence_type(self, ev_id: str) -> EvidenceType | None:
        return next((e for e in self.evidence if e.id == ev_id), None)

    def check(self, check_id: str) -> Check | None:
        return next((c for c in self.checks if c.id == check_id and c.enabled), None)

    def describe(self) -> str:
        lines = [f"Framework: {self.name} ({self.ref})", f"Purpose: {self.purpose}", "", "Scales (lowest to highest):"]
        lines += [f"  {i}. {s.id} — {s.name}" + (f": {s.description}" if s.description else "")
                  for i, s in enumerate(self.scales)]
        lines += ["", "Relation types:"]
        lines += [f"  - {r.id} [{r.kind}{', sign ' + format(r.sign, '+d') if r.sign else ''}]: {r.description}"
                  for r in self.relations]
        lines += ["", f"Context fields: {', '.join(self.context_fields)}",
                  f"Fields that must match to compare claims: {', '.join(self.context_match) or '-'}", "",
                  "Evidence types:"]
        lines += [f"  - {e.id} (weight {e.weight}{', can support causal claims' if e.supports_causal else ''}): "
                  f"{e.description}" for e in self.evidence]
        if self.questions:
            lines += ["", "Questions this framework addresses:"] + [f"  - {q}" for q in self.questions]
        lines += ["", "Checks: " + ", ".join(c.id for c in self.checks if c.enabled)]
        return "\n".join(lines)

    def validate(self) -> list[str]:
        errors = []
        ids = [s.id for s in self.scales]
        if len(ids) < 2 or len(set(ids)) != len(ids):
            errors.append("need at least two scales with unique ids")
        for r in self.relations:
            if r.kind not in RELATION_KINDS:
                errors.append(f"relation {r.id}: kind must be one of {RELATION_KINDS}")
            if r.sign not in (-1, 0, 1):
                errors.append(f"relation {r.id}: sign must be -1, 0 or 1")
        for c in self.checks:
            if c.id not in KNOWN_CHECKS:
                errors.append(f"unknown check {c.id}; known: {KNOWN_CHECKS}")
        for f in self.context_match:
            if f not in self.context_fields:
                errors.append(f"context_match field {f} is not in context_fields")
        if not any(e.supports_causal for e in self.evidence):
            errors.append("no evidence type can support a causal claim")
        return errors


def load_framework_file(path: Path) -> Framework:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    guide = ""
    if data.get("guide"):
        gpath = path.parent / data["guide"]
        guide = gpath.read_text(encoding="utf-8") if gpath.exists() else ""
    checks = []
    for c in data.get("checks", [{"id": k} for k in KNOWN_CHECKS]):
        params = {k: v for k, v in c.items() if k not in ("id", "enabled", "description")}
        checks.append(Check(c["id"], bool(c.get("enabled", True)), params, c.get("description", "")))
    fw = Framework(
        id=data.get("id") or path.stem,
        version=int(data.get("version", 1)),
        name=data.get("name", data.get("id", path.stem)),
        purpose=data.get("purpose", ""),
        scales=[Scale(s["id"], s.get("name", s["id"]), s.get("description", "")) for s in data.get("scales", [])],
        relations=[Relation(r["id"], r.get("kind", "associative"), int(r.get("sign", 0)), r.get("description", ""))
                   for r in data.get("relations", DEFAULT_RELATIONS)],
        context_fields=list(data.get("context_fields", ["species", "cell_type", "tissue", "intervention", "time"])),
        context_match=list(data.get("context_match", [])),
        evidence=[EvidenceType(e["id"], float(e.get("weight", 0.5)), bool(e.get("supports_causal", False)),
                               e.get("description", "")) for e in data.get("evidence", DEFAULT_EVIDENCE)],
        questions=list(data.get("questions", [])),
        checks=checks,
        views=list(data.get("views", [])),
        guide=guide,
        path=path,
    )
    errors = fw.validate()
    if errors:
        raise SystemExit(f"cairn: invalid framework {path}:\n  " + "\n  ".join(errors))
    return fw
