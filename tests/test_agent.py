import json
from types import SimpleNamespace as NS

from cairn.agent import Claude, Organizer, _echoable
from cairn.indexer import build_index


def text(t):
    return NS(type="text", text=t)


def tool_use(i, name, args):
    return NS(type="tool_use", id=i, name=name, input=args)


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responder(kw, len(self.calls))


def fake_client(responder):
    msgs = FakeMessages(responder)
    return NS(beta=NS(messages=msgs)), msgs


def structured_responder(kw, n):
    schema = kw["output_config"]["format"]["schema"]
    props = schema["properties"]
    if "key_claims" in props:
        out = {"summary": "A summary.", "topics": ["memory"], "note_type": "concept",
               "key_claims": ["Claim."], "open_questions": ["Why?"], "entities": []}
    elif "relations" in props:
        prompt = kw["messages"][0]["content"]
        cands = [line[4:] for line in prompt.split("CANDIDATES", 1)[1].splitlines() if line.startswith("id: ")]
        out = {"relations": [{"candidate": cands[0], "type": "supports", "reason": "Shared mechanism.",
                              "confidence": 0.9}] + [{"candidate": c, "type": "none", "reason": "-",
                                                       "confidence": 0.9} for c in cands[1:]]}
    else:
        out = {"name": "Named Topic", "synopsis": "What these notes establish.", "open_questions": ["Q?"]}
    return NS(stop_reason="end_turn", content=[text(json.dumps(out))])


def test_bulk_passes_are_incremental(indexed):
    cfg, store = indexed
    client, msgs = fake_client(structured_responder)
    org = Organizer(cfg, store, Claude(cfg, client), log=lambda m: None)
    assert org.enrich() == 13
    assert all(c["fallbacks"] == "default" and c["model"] == cfg.model for c in msgs.calls)
    assert org.enrich() == 0  # nothing changed
    (cfg.root / "sleep/naps.md").write_text("# Naps\n\nChanged content about naps.\n")
    build_index(cfg, store)
    assert org.enrich() == 1

    added = org.link()
    assert added > 0
    assert all(r["type"] == "supports" for r in store.relations())
    before = len(msgs.calls)
    org.link()
    assert len(msgs.calls) == before  # candidates unchanged -> no new calls

    build_index(cfg, store)
    assert org.name_topics() >= 2
    assert all(t["label"] == "Named Topic" and not t["stale"] for t in store.topics())
    build_index(cfg, store)
    assert org.name_topics() == 0  # names survive a rebuild


def test_agent_loop_uses_tools_and_writes_cited_note(indexed):
    cfg, store = indexed

    def responder(kw, n):
        if n == 1:
            return NS(stop_reason="tool_use", content=[tool_use("t1", "search", {"query": "caffeine sleep"}),
                                                       tool_use("t2", "read_note", {"note_id": "nope-nope"})])
        if n == 2:
            results = kw["messages"][-1]["content"]
            assert results[0]["tool_use_id"] == "t1" and "sleep/caffeine" in results[0]["content"]
            assert results[1]["is_error"] is True
            return NS(stop_reason="tool_use", content=[tool_use("t3", "write_note", {
                "kind": "insights", "title": "Coffee undermines studying", "body": "Because [[sleep/caffeine]]...",
                "sources": ["sleep/caffeine", "sleep/sleep-and-memory"]})])
        return NS(stop_reason="end_turn", content=[text("Wrote one insight.")])

    client, msgs = fake_client(responder)
    org = Organizer(cfg, store, Claude(cfg, client), log=lambda m: None)
    events = []
    summary, written = org.insights(n=1, on_event=events.append)
    assert summary == "Wrote one insight."
    assert len(written) == 1 and written[0].startswith("_cairn/insights/")
    assert events[0].startswith("search(")
    tool_names = {t["name"] for t in msgs.calls[0]["tools"]}
    assert {"write_note", "add_relation", "search"} <= tool_names


def test_ask_is_read_only_without_save(indexed):
    cfg, store = indexed
    client, msgs = fake_client(lambda kw, n: NS(stop_reason="end_turn", content=[text("Answer [[sleep/caffeine]]")]))
    answer, written = Organizer(cfg, store, Claude(cfg, client)).ask("Does coffee affect memory?")
    assert answer.startswith("Answer") and written == []
    assert "write_note" not in {t["name"] for t in msgs.calls[0]["tools"]}
    assert "Does coffee affect memory?" in msgs.calls[0]["messages"][0]["content"]


def test_turn_budget_forces_final_answer(indexed):
    cfg, store = indexed

    def responder(kw, n):
        if kw["tool_choice"]["type"] == "none":
            return NS(stop_reason="end_turn", content=[text("final")])
        return NS(stop_reason="tool_use", content=[tool_use(f"t{n}", "overview", {})])

    client, _ = fake_client(responder)
    from cairn.tools import VaultTools
    out = Claude(cfg, client).run_agent("go", VaultTools(cfg, store), max_turns=3)
    assert out == "final"


def test_echoable_drops_pre_fallback_internal_blocks():
    content = [NS(type="thinking"), text("partial"), NS(type="tool_use"), NS(type="fallback"), text("after")]
    assert [b.type for b in _echoable(content)] == ["text", "text"]
    assert _echoable([text("x")])[0].text == "x"


def test_signals(indexed):
    cfg, store = indexed
    store.add_relation("work/multitasking", "work/deep-work", "contradicts", "switching cost")
    sig = Organizer(cfg, store, llm=None).signals()
    assert sig["contradictions"][0]["src"] == "work/multitasking"


def test_failed_call_is_logged_and_skipped(indexed):
    cfg, store = indexed

    def responder(kw, n):
        if "Caffeine" in kw["messages"][0]["content"]:
            raise RuntimeError("boom")
        return structured_responder(kw, n)

    client, _ = fake_client(responder)
    logs = []
    org = Organizer(cfg, store, Claude(cfg, client), log=logs.append)
    assert org.enrich() == 12
    assert any("FAILED sleep/caffeine" in m for m in logs)
    assert store.enrichment("sleep/caffeine") is None
