from pathlib import Path

from cairn.vault import MANAGED_END, MANAGED_START, Resolver, extract_links, extract_tags, load_vault, parse_note


def test_links_variants_and_code_is_ignored():
    body = """See [[Note A|alias]], [[folder/b#Heading]], ![[img.png]] and [c](sub/c.md#x).
External [site](https://example.com) is skipped.
```
[[not-a-link]]
```
and `[[inline-code]]`."""
    links = extract_links(body)
    assert [(l.target, l.anchor, l.text, l.kind) for l in links] == [
        ("Note A", "", "alias", "wiki"),
        ("folder/b", "Heading", "", "wiki"),
        ("img.png", "", "", "embed"),
        ("sub/c.md", "x", "c", "markdown"),
    ]


def test_tags_from_frontmatter_and_body_not_headings_or_code():
    tags = extract_tags("# Heading\nText #idea and #project/alpha.\n`#nope`\n", {"tags": ["Learning"]})
    assert tags == ["learning", "idea", "project/alpha"]


def test_parse_note_strips_managed_block(tmp_path: Path):
    p = tmp_path / "n.md"
    p.write_text(f"---\ntitle: Custom\naliases: My Alias\n---\nBody [[x]]\n\n{MANAGED_START}\n[[y]]\n{MANAGED_END}\n")
    n = parse_note(p, tmp_path)
    assert n.title == "Custom" and n.aliases == ["My Alias"]
    assert [l.target for l in n.links] == ["x"]
    assert MANAGED_START not in n.body


def test_hash_ignores_managed_block(tmp_path: Path):
    p = tmp_path / "n.md"
    p.write_text("Body\n")
    h1 = parse_note(p, tmp_path).hash
    p.write_text(f"Body\n\n{MANAGED_START}\nstuff\n{MANAGED_END}\n")
    assert parse_note(p, tmp_path).hash == h1


def test_resolver(vault):
    notes = load_vault(vault)
    r = Resolver(notes)
    assert r.resolve("spaced-repetition") == "learning/spaced-repetition"
    assert r.resolve("SRS") == "learning/spaced-repetition"  # alias
    assert r.resolve("Deep work") == "work/deep-work"  # title
    assert r.resolve("maker-schedule.md", "work/deep-work") == "work/maker-schedule"  # relative md link
    assert r.resolve("statistics-syllabus") is None


def test_sections(vault):
    n = parse_note(vault / "learning/spaced-repetition.md", vault)
    assert [s.heading for s in n.sections] == ["Spaced repetition", "How I use it", "Caveat"]
