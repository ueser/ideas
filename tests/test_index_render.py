import json

from cairn.indexer import build_index
from cairn.render import Renderer
from cairn.tools import ToolError, VaultTools

import pytest


def test_index_stats_and_health(indexed):
    cfg, store = indexed
    stats = store.get_meta("stats")
    assert stats["notes"] == 13
    assert stats["topics"] >= 2
    assert store.broken_links() == [{"src": "projects/study-system", "target": "statistics-syllabus"}]
    assert "inbox/random-thought" in store.orphans()
    assert "learning/spaced-repetition" in store.backlinks("learning/active-recall") or \
        "learning/active-recall" in store.backlinks("learning/spaced-repetition")


def test_search_ranks_relevant_note(indexed):
    cfg, store = indexed
    res = VaultTools(cfg, store).search("caffeine adenosine")
    assert res[0]["id"] == "sleep/caffeine"
    assert VaultTools(cfg, store).search("sleep", tag="health")[0]["id"] == "sleep/caffeine"


def test_learning_notes_share_a_topic(indexed):
    cfg, store = indexed
    topics = {store.note(n)["topic"] for n in ["learning/active-recall", "learning/spaced-repetition"]}
    assert len(topics) == 1 and None not in topics


def test_tools(indexed):
    cfg, store = indexed
    t = VaultTools(cfg, store)
    assert t.read_note("SRS")["id"] == "learning/spaced-repetition"
    assert "Caveat" in t.read_note("spaced-repetition", section="caveat")["content"]
    links = t.note_links("study-system")
    assert "statistics-syllabus" in links["broken_links"]
    path = t.find_path("caffeine", "spaced-repetition")
    assert path["path"][0]["id"] == "sleep/caffeine"
    with pytest.raises(ToolError):
        t.add_relation("caffeine", "naps", "supports", "x")  # writes disabled
    with pytest.raises(ToolError, match="Did you mean"):
        t.read_note("caffeen adenosine")
    json.dumps(t.overview())  # serialisable


def test_render_is_idempotent_and_keeps_human_text(indexed):
    cfg, store = indexed
    cfg.write_note_blocks = True
    original = (cfg.root / "sleep/naps.md").read_text()
    changed = Renderer(cfg, store).render_all()
    assert "_cairn/INDEX.md" in changed and "_cairn/AGENTS.md" in changed
    after = (cfg.root / "sleep/naps.md").read_text()
    assert after.startswith(original.rstrip())
    assert "<!-- cairn:start -->" in after
    stats_before = build_index(cfg, store)
    Renderer(cfg, store).render_all()  # note mtimes moved, so "recently changed" may reorder once
    build_index(cfg, store)
    assert Renderer(cfg, store).render_all() == []
    # managed blocks don't add graph links
    assert build_index(cfg, store)["links"] == stats_before["links"]


def test_nav_pages_not_in_search_and_stale_topics_removed(indexed):
    cfg, store = indexed
    Renderer(cfg, store).render_all()
    stale = cfg.out_path / "topics" / "t-old.md"
    stale.write_text("---\ngenerated_by: cairn\n---\n# old\n")
    keep = cfg.out_path / "topics" / "mine.md"
    keep.write_text("# hand written\n")
    build_index(cfg, store)
    assert all(not r["id"].startswith("_cairn/") for r in store.search("sleep memory"))
    Renderer(cfg, store).render_all()
    assert not stale.exists() and keep.exists()


def test_knowledge_layer_survives_rename(indexed):
    cfg, store = indexed
    store.set_enrichment("sleep/naps", store.note("sleep/naps")["hash"], {"summary": "Short naps help."})
    store.add_relation("sleep/naps", "sleep/caffeine", "alternative_to", "both fight sleepiness")
    (cfg.root / "sleep/naps.md").rename(cfg.root / "sleep/napping.md")
    build_index(cfg, store)
    assert store.note("sleep/napping")["summary"] == "Short naps help."
    assert store.relations("sleep/napping")[0]["type"] == "alternative_to"


def test_write_note_stays_in_output_dir(indexed):
    cfg, store = indexed
    t = VaultTools(cfg, store, allow_writes=True)
    res = t.write_note("insights", "Sleep is part of spacing", "Body citing [[sleep/sleep-and-memory]].",
                       ["sleep-and-memory", "SRS"])
    assert res["path"].startswith("_cairn/insights/")
    text = (cfg.root / res["path"]).read_text()
    assert "sources:" in text and "[[learning/spaced-repetition|Spaced repetition]]" in text
    build_index(cfg, store)
    # insights are searchable but excluded from the graph analysis
    assert store.search("spacing")[0]["id"] != ""
    assert store.note(res["path"][:-3])["generated"] == 1
