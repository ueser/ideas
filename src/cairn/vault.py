"""Parse a folder of markdown notes into structured Note objects.

Everything here is deterministic and dependency-free (PyYAML is used for
frontmatter when available, with a small fallback parser otherwise).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

# Blocks written by cairn inside human notes. They are stripped before
# analysis so generated content never feeds back into the graph.
MANAGED_START = "<!-- cairn:start -->"
MANAGED_END = "<!-- cairn:end -->"
MANAGED_RE = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), re.S)

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]|#]*)(#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
TAG_RE = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w\-/]*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)

DEFAULT_EXCLUDE = [".git", ".cairn", ".obsidian", ".trash", "node_modules", ".venv"]


@dataclass
class Link:
    target: str  # raw target as written, without heading/alias
    anchor: str = ""  # heading anchor, if any
    text: str = ""  # alias / link text
    kind: str = "wiki"  # wiki | markdown | embed


@dataclass
class Section:
    heading: str  # "" for the preamble before the first heading
    level: int
    text: str
    anchor: str


@dataclass
class Note:
    id: str  # vault-relative posix path without the .md extension
    path: Path
    title: str
    frontmatter: dict
    body: str  # body with frontmatter and managed blocks removed
    tags: list[str]
    links: list[Link]
    sections: list[Section]
    aliases: list[str]
    mtime: float
    hash: str
    generated: bool = False  # lives in cairn's output dir
    word_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.word_count = len(re.findall(r"\w+", self.body))


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", text) or "untitled"


def _simple_yaml(text: str) -> dict:
    """Tiny YAML subset: `key: value`, inline `[a, b]` lists and `- item` lists."""
    data: dict = {}
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s+-\s*(.*)$", line) or re.match(r"^-\s*(.*)$", line)
        if m and current is not None:
            if not isinstance(data.get(current), list):
                data[current] = []
            data[current].append(m.group(1).strip().strip("'\""))
            continue
        m = re.match(r"^([\w\-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        current = key
        if val.startswith("[") and val.endswith("]"):
            data[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val == "":
            data[key] = None
        else:
            data[key] = val.strip("'\"")
    return data


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    data: dict = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            data = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            data = _simple_yaml(raw)
    else:
        data = _simple_yaml(raw)
    return data, text[m.end():]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,\s]+", value) if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _aliases(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return _as_list(value)


def strip_code(text: str) -> str:
    return INLINE_CODE_RE.sub(" ", FENCE_RE.sub(" ", text))


def extract_links(body: str) -> list[Link]:
    text = strip_code(body)
    links: list[Link] = []
    for m in WIKILINK_RE.finditer(text):
        target = m.group(2).strip()
        anchor = (m.group(3) or "").lstrip("#").strip()
        if not target and not anchor:
            continue
        links.append(Link(target=target, anchor=anchor, text=(m.group(4) or "").strip(),
                          kind="embed" if m.group(1) else "wiki"))
    for m in MDLINK_RE.finditer(text):
        href = m.group(2)
        if re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.I):  # http:, mailto:, ...
            continue
        target, _, anchor = href.partition("#")
        if not target or not (target.endswith(".md") or "." not in Path(target).name):
            continue
        from urllib.parse import unquote
        links.append(Link(target=unquote(target), anchor=anchor, text=m.group(1), kind="markdown"))
    return links


def extract_tags(body: str, frontmatter: dict) -> list[str]:
    tags = [t.lstrip("#") for t in _as_list(frontmatter.get("tags") or frontmatter.get("tag"))]
    text = HEADING_RE.sub(" ", strip_code(body))
    tags += TAG_RE.findall(text)
    seen, out = set(), []
    for t in tags:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def extract_sections(body: str) -> list[Section]:
    sections: list[Section] = []
    # find headings outside code fences
    masked = FENCE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), body)
    matches = list(HEADING_RE.finditer(masked))
    start = 0
    heading, level = "", 0
    for m in matches:
        sections.append(Section(heading, level, body[start:m.start()].strip(), slugify(heading) if heading else ""))
        heading, level, start = m.group(2).strip(), len(m.group(1)), m.end()
    sections.append(Section(heading, level, body[start:].strip(), slugify(heading) if heading else ""))
    return [s for s in sections if s.text or s.heading]


def parse_note(path: Path, root: Path, generated_dir: str | None = None) -> Note:
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(raw)
    body = MANAGED_RE.sub("", body).strip()
    rel = path.relative_to(root).as_posix()
    note_id = rel[:-3] if rel.lower().endswith(".md") else rel
    title = str(fm.get("title") or "").strip()
    if not title:
        h1 = re.search(r"^#\s+(.+?)\s*$", body, re.M)
        title = h1.group(1).strip() if h1 else path.stem
    return Note(
        id=note_id,
        path=path,
        title=title,
        frontmatter=fm,
        body=body,
        tags=extract_tags(body, fm),
        links=extract_links(body),
        sections=extract_sections(body),
        aliases=_aliases(fm.get("aliases") or fm.get("alias")),
        mtime=path.stat().st_mtime,
        hash=hashlib.sha256(body.encode()).hexdigest()[:16],
        generated=bool(generated_dir) and (rel == generated_dir or rel.startswith(generated_dir + "/")),
    )


def iter_markdown(root: Path, exclude: list[str] | None = None):
    patterns = list(DEFAULT_EXCLUDE) + list(exclude or [])
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames
            if not any(fnmatch.fnmatch(d, p) or fnmatch.fnmatch(f"{rel_dir}/{d}".lstrip("./"), p) for p in patterns)
        )
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            rel = f"{rel_dir}/{name}".lstrip("./") if rel_dir != "." else name
            if any(fnmatch.fnmatch(rel, p) for p in patterns):
                continue
            yield Path(dirpath) / name


def load_vault(root: Path, exclude: list[str] | None = None, generated_dir: str | None = None) -> list[Note]:
    return [parse_note(p, root, generated_dir) for p in iter_markdown(root, exclude)]


class Resolver:
    """Resolve link targets to note ids the way Obsidian-style tools do."""

    def __init__(self, notes: list[Note]):
        self.by_id = {n.id.lower(): n.id for n in notes}
        self.by_stem: dict[str, list[str]] = {}
        self.by_title: dict[str, str] = {}
        for n in notes:
            self.by_stem.setdefault(n.path.stem.lower(), []).append(n.id)
            self.by_title.setdefault(n.title.lower(), n.id)
            for a in n.aliases:
                self.by_title.setdefault(a.lower(), n.id)

    def resolve(self, target: str, source_id: str | None = None) -> str | None:
        t = target.strip().replace("\\", "/")
        if t.lower().endswith(".md"):
            t = t[:-3]
        if not t:
            return source_id  # same-note anchor link
        key = t.lower().lstrip("/")
        # relative to the source note's folder (markdown-style links)
        if source_id is not None and ("/" in t or t.startswith(".")):
            base = Path(source_id).parent
            rel = os.path.normpath((base / t).as_posix()).replace("\\", "/").lower()
            if rel in self.by_id:
                return self.by_id[rel]
        if key in self.by_id:
            return self.by_id[key]
        stem = Path(key).name
        cands = self.by_stem.get(stem)
        if cands:
            if len(cands) == 1 or source_id is None:
                return cands[0]
            # prefer the candidate sharing the longest folder prefix with the source
            src_parts = Path(source_id).parts
            return max(cands, key=lambda c: len(os.path.commonprefix([Path(c).parts, src_parts])))
        return self.by_title.get(key)
