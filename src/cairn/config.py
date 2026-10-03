"""Vault configuration, read from an optional `cairn.toml` at the vault root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    root: Path
    output_dir: str = "_cairn"  # where generated navigation notes live
    exclude: list[str] = field(default_factory=list)  # extra glob patterns to skip
    link_style: str = "wiki"  # wiki ([[id|Title]]) or markdown ([Title](path.md))
    write_note_blocks: bool = False  # append a managed "connections" block to each note
    related_k: int = 6  # related notes computed per note
    min_similarity: float = 0.12  # similarity floor for "related" edges
    model: str = "claude-opus-5-5"
    effort: str = "medium"  # effort for bulk per-note calls (enrich / link)
    agent_effort: str = "high"  # effort for exploratory agent runs (ask / insights)
    max_workers: int = 4  # parallel LLM calls for bulk passes
    framework: str = ""  # path (relative to the vault) of a framework TOML; enables `cairn research`
    research_dir: str = "_research"  # claims, mappings, cases, narratives, questions, views

    @property
    def state_dir(self) -> Path:
        return self.root / ".cairn"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "index.db"

    @property
    def research_path(self) -> Path:
        return self.root / self.research_dir

    @property
    def out_path(self) -> Path:
        return self.root / self.output_dir

    def load_framework(self):
        """The configured Framework, or None."""
        if not self.framework:
            return None
        from .research.framework import load_framework_file
        path = Path(self.framework).expanduser()
        path = path if path.is_absolute() else self.root / path
        if not path.exists():
            raise SystemExit(f"cairn: framework file not found: {path}")
        return load_framework_file(path)


def load_config(root: str | Path) -> Config:
    root = Path(root).resolve()
    if not root.is_dir():
        raise SystemExit(f"cairn: vault folder not found: {root}")
    cfg = Config(root=root)
    toml_path = root / "cairn.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text())
        data = data.get("cairn", data)
        known = {f.name for f in fields(Config)} - {"root"}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
    cfg.output_dir = cfg.output_dir.strip("/")
    cfg.research_dir = cfg.research_dir.strip("/")
    return cfg
