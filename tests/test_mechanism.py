import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cairn.agent import Claude
from cairn.config import load_config
from cairn.indexer import build_index
from cairn.mech_agent import MechanismOrganizer, claim_schema
from cairn.mechanism import Claim, MechanismModel, Pair, norm_name
from cairn.render import Renderer
from cairn.store import Store
from cairn.tools import VaultTools, tool_definitions

from mech_fixtures import responder

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class Msgs:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def create(self, **kw):
        self.calls.append(kw)
        return self.fn(kw, len(self.calls))


@pytest.fixture
def mech(tmp_path):
    shutil.copytree(EXAMPLES / "disease-vault", tmp_path / "vault")
    shutil.copytree(EXAMPLES / "frameworks", tmp_path / "frameworks")
    cfg = load_config(tmp_path / "vault")
    store = Store(cfg.db_path)
    build_index(cfg, store)
    msgs = Msgs(responder)
    org = MechanismOrganizer(cfg, store, Claude(cfg, NS(beta=NS(messages=msgs))), cfg.load_framework(),
                             log=lambda m: None)
    yield cfg, store, org, msgs
    store.close()


def test_sign_algebra():
    c = Claim(1, "n", "A", "gene", "decrease", "correlative", "W", "condition", "increase", "genetic", "", "", 1.0)
    assert c.sign == -1
    assert norm_name("TGF-β1") == norm_name("tgf beta 1") == "tgfbeta1"


def test_contested_pair():
    mk = lambda note, sc: Claim(1, note, "A", "protein", "increase", "causal", "B", "protein", sc, "intervention",
                                "", "", 1.0, weight=0.9)
    p = Pair("A", "B", [mk("n1", "increase"), mk("n2", "decrease")])
    assert p.contested and p.sign == 0
    p = Pair("A", "B", [mk("n1", "increase"), mk("n2", "increase")])
    assert p.sign == 1 and p.evidence == pytest.approx(0.99)  # noisy-OR over independent notes


def test_extraction_is_incremental_and_schema_is_framework_specific(mech):
    cfg, store, org, msgs = mech
    assert org.extract() == 16
    assert org.extract() == 0
    schema = msgs.calls[0]["output_config"]["format"]["schema"]
    assert schema == claim_schema(org.fw)
    assert "condition" in schema["properties"]["claims"]["items"]["properties"]["object_scale"]["enum"]
    assert "Multi-scale disease mechanism" in msgs.calls[0]["system"]


def test_triangulation_finds_the_incoherent_triangle(mech):
    cfg, store, org, _ = mech
    org.extract()
    org.normalize()
    m = MechanismModel(store, org.fw)
    assert "SIG signaling" not in m.entities and "SIG pathway" in m.entities  # merged synonym
    assert m.entities["KRN1"].scale == "protein"  # majority vote over gene/protein mentions
    assert "Nephropathy Z" in m.entities or "nephropathy Z" in m.entities  # case variants merged
    bad = [set(c.nodes) for c in m.cycles if not c.coherent]
    tri = {"KRN1", "myofibroblast activation", m.find_entity("nephropathy z").name}
    assert tri in bad
    rep = m.consistency()
    nz = m.find_entity("nephropathy z").name
    loop = next(l for l in rep["incoherent_loops"] if set(l["loop"]) == tri)
    # the genetic association sits in the most incoherent loops, so it is the prime suspect
    assert set(loop["suspect_link"]) == {"KRN1", nz}
    assert set(rep["suspect_links"][0]["between"]) == {"KRN1", nz}
    # coherent loops raise confidence: SIG–myofibroblast–fibrosis triangle is balanced
    assert m.pairs[("SIG pathway", "myofibroblast activation")].coherent >= 1


def test_chain_climbs_the_scales(mech):
    cfg, store, org, _ = mech
    org.extract()
    org.normalize()
    m = MechanismModel(store, org.fw)
    target = m.find_entity("nephropathy Z").name
    assert m.default_targets(1) == [target]
    best = m.chains(target, k=3)[0]
    levels = [m.level(n) for n in best["chain"]]
    assert levels == sorted(levels)
    assert best["chain"][0] == "KRN1 kinase domain C-lobe"
    assert {"protein_domain", "complex", "pathway", "cell_program", "tissue", "condition"} <= set(best["scales_covered"])
    assert any("only correlative" in p or "skips" in p for p in best["open_points"])
    from_krn1 = m.chains(target, source="KRN1")
    assert from_krn1 and all(c["chain"][0] == "KRN1" for c in from_krn1)


def test_tools_render_and_agent_surface(mech):
    cfg, store, org, _ = mech
    org.extract()
    org.normalize()
    tools = VaultTools(cfg, store)
    names = {t["name"] for t in tool_definitions(tools)}
    assert {"mechanism_overview", "mechanism_entity", "mechanism_chains", "mechanism_consistency"} <= names
    ent = tools.mechanism_entity("krn1")
    assert {u["entity"] for u in ent["downstream"]} >= {"myofibroblast activation", "SIG pathway"}
    assert ent["incoherent_loops"]
    cfg.write_note_blocks = True
    changed = Renderer(cfg, store).render_all()
    base = f"{cfg.output_dir}/{org.fw.id}"
    for page in ("index.md", "chains.md", "consistency.md", "scales/cell_program.md", "entities/krn1.md"):
        assert f"{base}/{page}" in changed
    idx = (cfg.root / base / "index.md").read_text()
    assert "```mermaid" in idx and "incoherent" in idx
    assert "Multi-scale disease mechanism" in (cfg.out_path / "INDEX.md").read_text()
    assert "[[_cairn/multi-scale-disease-mechanism/entities/krn1|KRN1]]" in \
        (cfg.root / "papers/nakamura-2023-gwas.md").read_text()
    build_index(cfg, store)
    hits = store.search("KRN1 macrophage")
    assert hits and not any(h["id"].endswith(("/index", "/chains", "/consistency")) for h in hits)


def test_no_framework_means_no_mech_tools(indexed):
    cfg, store = indexed
    assert not any(t["name"].startswith("mechanism") for t in tool_definitions(VaultTools(cfg, store)))


def test_narrate_agent_gets_chains_and_writes_cited_note(mech):
    cfg, store, org, _ = mech
    org.extract()
    org.normalize()
    calls = []

    def agent(kw, n):
        calls.append(kw)
        if n == 1:
            assert "KRN1 kinase domain C-lobe" in kw["messages"][0]["content"]  # chains are in the task
            assert "suspect_link" in kw["messages"][0]["content"]  # so are the incoherent loops
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="mechanism_entity",
                                                          input={"name": "KRN1"})])
        if n == 2:
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t2", name="write_note", input={
                "kind": "mechanisms", "title": "Mechanism of nephropathy Z", "body": "KRN1 → … [[papers/lee-2021-krn1-myofibroblast]]",
                "sources": ["papers/lee-2021-krn1-myofibroblast", "papers/nakamura-2023-gwas"]})])
        return NS(stop_reason="end_turn", content=[NS(type="text", text="done")])

    org.llm = Claude(cfg, NS(beta=NS(messages=Msgs(agent))))
    text, written = org.narrate()
    assert text == "done" and written[0].startswith("_cairn/mechanisms/")
    assert "mechanism_chains" in {t["name"] for t in calls[0]["tools"]}
    assert '"downstream"' in calls[1]["messages"][2]["content"][0]["content"]  # tool result came back
