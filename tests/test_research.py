import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cairn.agent import Claude
from cairn.config import load_config
from cairn.indexer import build_index
from cairn.research import narratives as N
from cairn.research.evaluate import evaluate
from cairn.research.extract import Extractor, merge_claims
from cairn.research.framework import load_framework_file
from cairn.research.records import NOTES_MARK, Records, locate, study_identity
from cairn.research.workspace import Workspace
from cairn.store import Store
from cairn.tools import VaultTools, tool_definitions

from research_fake import client

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
Q = "Which paths connect a KRN1 perturbation to nephropathy Z?"


@pytest.fixture
def ws(tmp_path):
    shutil.copytree(EXAMPLES / "eval-vault", tmp_path / "vault")
    shutil.copytree(EXAMPLES / "frameworks", tmp_path / "frameworks")
    cfg = load_config(tmp_path / "vault")
    store = Store(cfg.db_path)
    build_index(cfg, store)
    w = Workspace(cfg, store)
    w.sync()
    c, msgs = client()
    w.extractor = Extractor(cfg, store, Claude(cfg, c), w.fw, log=lambda m: None)
    w.msgs = msgs
    yield w
    store.close()


def extracted(w):
    w.extractor.extract()
    w.extractor.normalize()
    w.sync()
    w.check()
    return w


def reindex(w):
    build_index(w.cfg, w.store)
    return w.sync()


# ---- step 1: records -----------------------------------------------------------

def test_locate_and_study_identity():
    text = "# T\n\n## Results\nIn human cells, KRN1\nknockdown reduced X.\n"
    span = locate("KRN1 knockdown reduced X", text)
    assert span["match"] == "exact" and (span["line_start"], span["line_end"]) == (4, 5)
    assert span["heading"] == "Results"
    assert locate("KRN1 knockdwn reduced X.", text)["match"].startswith("fuzzy")
    assert locate("completely different sentence about mice", text) is None
    assert study_identity({"doi": "10.1/ABC"}, "", "n")["id"] == "doi:10.1/abc"
    assert study_identity({}, "Lee et al. 2021", "n")["id"] == study_identity({"source": "Lee et al 2021"}, "", "m")["id"]
    assert study_identity({}, "", "n")["basis"] == "unknown"


def test_claims_are_jsonl_per_note_with_passages(ws):
    extracted(ws)
    path = ws.cfg.research_path / "records" / "claims" / "papers" / "lee-2021-fibroblast.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 2 and all(r["status"] == "proposed" for r in rows)
    r = rows[0]
    assert r["span"]["match"] == "exact" and r["span"]["heading"] == "Results"
    assert r["study"]["id"] == "doi:10.0000/synthetic.lee2021"
    assert r["framework"] == "disease-mechanism@2" and r["note_revision"]
    assert set(r["context"]) == set(ws.fw.context_fields)
    # mappings live apart from claims
    maps = ws.recs.mappings(ws.fw.id)
    assert maps["KRN1|activity"]["scales"] == ["protein"]
    assert maps["KRN1|loss-of-function variant"]["scales"] == ["gene"]


def test_reextraction_is_incremental_and_keeps_review_decisions(ws):
    extracted(ws)
    n_calls = len(ws.msgs.calls)
    assert ws.extractor.extract() == 0 and len(ws.msgs.calls) == n_calls
    cid = ws.recs.note_claims("papers/lee-2021-fibroblast")[0]["id"]
    ws.review("accept", [cid])
    ws.extractor.extract(force=True, notes=["papers/lee-2021-fibroblast"])
    assert Records(ws.cfg).claim(cid)["status"] == "accepted"  # same passage + content -> same id


def test_merge_rules():
    old = [{"id": "a", "status": "accepted"}, {"id": "b", "status": "proposed"}, {"id": "c", "status": "rejected"}]
    new = [{"id": "a", "status": "proposed"}, {"id": "d", "status": "proposed"}]
    merged = {c["id"]: c["status"] for c in merge_claims(old, new)}
    assert merged == {"a": "accepted", "d": "proposed", "c": "rejected"}


# ---- step 2: claim model ------------------------------------------------------------

def test_signs_assumptions_and_causal_demotion(ws):
    extracted(ws)
    m = ws.model()
    nak = next(l for l in m.links if l.claim["note"] == "papers/nakamura-2023-exome")
    assert nak.sign == -1 and nak.kind == "associative"
    assert any("inferred" in a for a in nak.assumptions)
    lee = next(l for l in m.links if l.claim["note"] == "papers/lee-2021-fibroblast" and "myofibroblast" in l.obj)
    assert lee.sign == 1 and lee.kind == "causal"  # KRN1 down, activation down
    assert any(l.kind == "null" for l in m.links)  # negations kept, not signed
    # a causally worded claim backed only by a statement is read as an association
    review = next(l for l in m.links if l.claim["note"] == "papers/review-gfr")
    assert review.kind == "associative"


# ---- steps 3-4: framework v2 and cases ---------------------------------------------

def test_framework_validation(tmp_path):
    p = tmp_path / "f.toml"
    p.write_text('id="x"\nversion=3\ncontext_fields=["species"]\ncontext_match=["cell_type"]\n'
                 '[[scales]]\nid="a"\n[[scales]]\nid="b"\n[[relations]]\nid="r"\nkind="magic"\nsign=1\n'
                 '[[checks]]\nid="nope"\n')
    with pytest.raises(SystemExit) as e:
        load_framework_file(p)
    msg = str(e.value)
    assert "kind must be one of" in msg and "unknown check nope" in msg and "context_match field cell_type" in msg


def test_potential_tension_case(ws):
    extracted(ws)
    d = next(d for d in ws.recs.docs("cases") if d["fm"]["case_type"] == "potential_tension"
             and set(d["fm"]["entities"]) == {"KRN1", "myofibroblast activation", "nephropathy Z"})
    body = d["body"]
    assert "**challenging claim**" in body and "nakamura-2023-exome" in body.split("**challenging claim**")[1][:400]
    assert "## Missing premises" in body and "only an association" in body
    assert "assumes loss-of-function variant of KRN1 lowers its activity" in body
    assert "responds to nephropathy Z rather than driving it" in body
    assert "confidence" not in d["fm"]  # cases never score


def test_feed_forward_and_conflict_and_triangulation(ws):
    extracted(ws)
    cases = {d["fm"]["case_type"]: d for d in ws.recs.docs("cases")}
    ff = [d for d in ws.recs.docs("cases") if d["fm"]["case_type"] == "conditional_sign_tension"]
    assert len(ff) == 1 and "Opposing causal routes: KRN1 → renal fibrosis" in ff[0]["fm"]["title"]
    assert "Opposing routes coexist" in ff[0]["body"]
    assert "differ in cell_type" in ff[0]["body"]
    conflict = cases["conflicting_result"]["body"]
    assert "an effect vs no effect" in conflict and "no effect:" in conflict and "species" in conflict
    tri = [d for d in ws.recs.docs("cases") if d["fm"]["case_type"] == "triangulation"]
    assert [d["fm"]["entities"] for d in tri] == [["SIG pathway", "myofibroblast activation"]]
    # the review statement about filtration does not count as independent support
    assert not any("glomerular" in d["fm"]["title"] for d in tri)


def test_one_study_in_two_notes_counts_once(ws):
    extracted(ws)
    m = ws.model()
    links = m.links_between("KRN1", "myofibroblast activation")
    assert len({l.claim["note"] for l in links}) == 2 and len({l.study for l in links}) == 1
    assert not any(set(d["fm"]["entities"]) == {"KRN1", "myofibroblast activation"}
                   for d in ws.recs.docs("cases") if d["fm"]["case_type"] == "triangulation")


# ---- step 5: review and staleness ---------------------------------------------------

def test_review_accept_note_revise_reject_and_stale_propagation(ws):
    extracted(ws)
    done = ws.review("accept", [], note="papers/chen-2019-biopsies", rationale="checked")
    assert len(done) == 2 and all(Records(ws.cfg).claim(c)["status"] == "accepted" for c in done)
    tension = next(d for d in ws.recs.docs("cases") if d["fm"]["case_type"] == "potential_tension"
                   and len(d["fm"]["entities"]) == 3)
    ws.review("accept", [tension["fm"]["id"]], rationale="pursue")
    # reviewer notes and the accepted status survive regeneration
    text = tension["path"].read_text().replace(NOTES_MARK, NOTES_MARK + "\nMy note: check variant function.")
    tension["path"].write_text(text)
    ws.check()
    again = Records(ws.cfg).doc("cases", tension["fm"]["id"])
    assert again["fm"]["status"] == "accepted" and "My note: check variant function." in again["body"]
    # revising a claim supersedes it; the case built on it goes back to review
    nak = ws.recs.note_claims("papers/nakamura-2023-exome")[0]["id"]
    new_id = ws.review("revise", [nak], sets=["subject.effect_basis=stated"], rationale="paper shows LoF")[0]
    recs = Records(ws.cfg)
    assert recs.claim(nak)["status"] == "superseded" and recs.claim(new_id)["supersedes"] == nak
    assert recs.claim(new_id)["status"] == "accepted"
    assert recs.doc("cases", tension["fm"]["id"])["fm"]["status"] == "stale"
    log = [r["action"] for r in recs.reviews()]
    assert log.count("accept") >= 3 and "revise" in log


def test_mapping_and_entity_review(ws):
    extracted(ws)
    ws.review("revise", ["map:KRN1|activity"], sets=["scales=protein,gene"])
    m = Records(ws.cfg).mappings(ws.fw.id)["KRN1|activity"]
    assert m["status"] == "accepted" and m["scales"] == ["protein", "gene"]
    with pytest.raises(SystemExit):
        ws.review("revise", ["map:KRN1|activity"], sets=["scales=organelle"])


def test_sync_follows_renames_and_marks_edits_and_removals(ws):
    extracted(ws)
    root = ws.cfg.root
    (root / "papers/lee-2021-fibroblast.md").rename(root / "papers/lee-renamed.md")
    chen = root / "papers/chen-2019-biopsies.md"
    chen.write_text(chen.read_text().replace("correlated with faster", "did not correlate with"))
    (root / "papers/santos-2022-mouse.md").unlink()
    rep = reindex(ws)
    assert rep["renamed"] == [["papers/lee-2021-fibroblast", "papers/lee-renamed"]]
    recs = Records(ws.cfg)
    assert len(recs.note_claims("papers/lee-renamed")) == 2
    statuses = {c["span"]["text"][:12]: c["status"] for c in recs.note_claims("papers/chen-2019-biopsies")}
    assert statuses["Fibrosis ext"] == "stale" and statuses["Kidney biops"] == "proposed"
    assert all(c["status"] == "source_missing" for c in recs.note_claims("papers/santos-2022-mouse"))
    assert rep["stale_docs"]  # the feed-forward case used the santos claim
    ws.check()  # the analysis is recomputed from live claims only
    assert not any("santos" in json.dumps(d["fm"]) for d in Records(ws.cfg).docs("cases")
                   if d["fm"]["status"] == "proposed")


# ---- step 6: questions, narratives, predictions ------------------------------------

def test_competing_narratives_without_llm(ws):
    extracted(ws)
    q = N.create_question(ws.recs, ws.fw, Q, "nephropathy Z", "KRN1")
    written = ws.narrate(q["id"])
    assert any(w.endswith("/a.md") for w in written) and any(w.endswith("comparison.md") for w in written)
    model = ws.model()
    pkg = N.compose_context(model, q, ws.case_records())
    a, b = pkg["narratives"]
    assert a["net_sign"] == 1 and b["net_sign"] == -1  # fibroblast route vs macrophage route
    statuses = {s["status"] for n in (a, b) for s in n["steps"]}
    assert "Supported" in statuses and "Assumed" in statuses
    assert any(c["type"] == "potential_tension" for c in a["challenges"])
    assert "gene" in a["coverage"]["scales_not_used"]
    comp = (ws.cfg.research_path / "narratives" / q["id"] / "comparison.md").read_text()
    assert "Reduce KRN1 and measure nephropathy Z" in comp and "Mediation test" in comp
    assert pkg["counterevidence"] and pkg["token_estimate"] > 0
    assert pkg["exclusions"] == {"claims_not_live": [], "unmapped_entities": []}


def test_missing_step_is_shown_not_filled(ws):
    extracted(ws)
    q = N.create_question(ws.recs, ws.fw, "Does ECM secretion lead to nephropathy Z?", "nephropathy Z",
                          "ECM secretion")
    paths = N.candidate_paths(ws.model(), q["target"], q["source"], {})
    assert paths[0][-1]["status"] == "Missing" and paths[0][-1]["to"] == "nephropathy Z"


def test_llm_narratives_are_validated(ws):
    extracted(ws)
    q = N.create_question(ws.recs, ws.fw, Q, "nephropathy Z", "KRN1")
    model = ws.model()
    real = next(l for l in model.links if l.subj == "KRN1" and l.obj == "SIG pathway")
    assoc = model.links_between("glomerular filtration", "nephropathy Z")[0]

    def synth(kw):
        return {"narratives": [
            {"label": "A", "title": "Fibroblast route", "summary": "s", "assumptions": [], "steps": [
                {"from": "KRN1", "to": "SIG pathway", "status": "Supported", "claims": [real.id, "c-0000000000"],
                 "text": "t"},
                {"from": "glomerular filtration", "to": "nephropathy Z", "status": "Supported",
                 "claims": [assoc.id], "text": "t"},
                {"from": "SIG pathway", "to": "nephropathy Z", "status": "Supported", "claims": [], "text": "leap"}]},
            {"label": "B", "title": "Macrophage route", "summary": "s", "assumptions": [], "steps": [
                {"from": "KRN1", "to": "nephropathy Z", "status": "Assumed", "claims": [real.id], "text": "t"}]}],
            "predictions": [{"observation": "o", "if_A": "a", "if_B": "b", "test": "t", "falsifies": "f"}]}

    c, _ = client(synth=synth)
    ws.narrate(q["id"], llm=Claude(ws.cfg, c))
    a = (ws.cfg.research_path / "narratives" / q["id"] / "a.md").read_text()
    assert "cites unknown claim(s) c-0000000000" in a
    assert "no causal claim in this direction; status set to Assumed" in a
    assert "no claim connects SIG pathway → nephropathy Z; status set to Missing" in a
    b = (ws.cfg.research_path / "narratives" / q["id"] / "b.md").read_text()
    assert "do not connect KRN1 and nephropathy Z" in b


# ---- agent surface ----------------------------------------------------------------------

def test_research_tools_and_bounded_investigation(ws):
    extracted(ws)
    q = N.create_question(ws.recs, ws.fw, Q, "nephropathy Z", "KRN1")
    tools = VaultTools(ws.cfg, ws.store)
    names = {t["name"] for t in tool_definitions(tools)}
    assert {"get_claim", "compose_context", "inspect_path", "list_cases"} <= names and "propose_case" not in names
    claim = tools.find_claims(entity="krn1", text="loss-of-function")[0]
    detail = tools.get_claim(claim["id"])
    assert "enriched in nephropathy Z" in detail["source_excerpt"] and detail["read_as"] == "associative"
    nak = claim["id"]

    def agent(kw, n):
        if n == 1:
            assert "propose_case" in {t["name"] for t in kw["tools"]}
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="get_claim",
                                                          input={"claim_id": nak})])
        if n == 2:
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t2", name="propose_case", input={
                "title": "Variant function untested", "claim_ids": [nak], "reasoning": "LoF is predicted only.",
                "explanations": "gain of function; dominant negative", "evidence_request": "functional assay"})])
        return NS(stop_reason="end_turn", content=[NS(type="text", text=f"Report citing {nak}.")])

    c, _ = client(agent=agent)
    report, written = ws.investigate(q["id"], Claude(ws.cfg, c), max_turns=5)
    assert report.startswith("Report") and any("investigation-" in w for w in written)
    assert any(w.startswith("_research/cases/agent-finding-") for w in written)
    inv = next(d for d in Records(ws.cfg).docs("questions") if d["fm"].get("type") == "investigation")
    assert inv["fm"]["inputs"] == [nak] and inv["fm"]["status"] == "proposed"


def test_views_and_eval(ws):
    extracted(ws)
    changed = ws.render()
    for p in ("index.md", "framework-map.md", "review-queue.md", "entities/krn1.md"):
        assert f"_research/views/{p}" in changed
    fmap = (ws.cfg.research_path / "views/framework-map.md").read_text()
    assert "No supporting material found in the indexed scope" in fmap  # e.g. no gene-only entity
    queue = (ws.cfg.research_path / "views/review-queue.md").read_text()
    assert "no context stated" in queue and "study identity unknown" in queue
    rep = evaluate(ws, EXAMPLES / "eval-vault" / "gold")
    assert rep["claims"]["precision"] == rep["claims"]["recall"] == 1.0
    assert all(c["found"] for c in rep["cases"])
    assert all(s["ok"] for s in rep["independence"])
    assert all(rep["negation_captured"].values()) and all(rep["unknown_context_kept_unknown"].values())
    assert rep["context"]["invented"] == 0


def test_eval_penalises_a_worse_extraction(ws):
    gold = json.loads((EXAMPLES / "eval-vault/gold/claims.json").read_text())
    worse = json.loads(json.dumps(gold))
    worse["papers/garcia-2023-rat"]["claims"][0]["negated"] = False  # negation lost
    worse["inbox/unknown-context"]["claims"][0]["context"]["species"] = "human"  # invented context
    c, _ = client(gold=worse)
    ws.extractor = Extractor(ws.cfg, ws.store, Claude(ws.cfg, c), ws.fw, log=lambda m: None)
    extracted(ws)
    rep = evaluate(ws, EXAMPLES / "eval-vault" / "gold")
    assert rep["claims"]["recall"] < 1.0
    assert rep["negation_captured"]["papers/garcia-2023-rat"] is False
    assert rep["unknown_context_kept_unknown"]["inbox/unknown-context"] is False


def test_cli_research_smoke(ws, capsys):
    from cairn.cli import main
    extracted(ws)
    v = str(ws.cfg.root)
    assert main(["-C", v, "research", "map"]) == 0
    assert "unmapped" in capsys.readouterr().out
    assert main(["-C", v, "research", "claims", "--entity", "KRN1", "--json"]) == 0
    cid = json.loads(capsys.readouterr().out)[0]["id"]
    assert main(["-C", v, "research", "claim", cid]) == 0
    assert "source:" in capsys.readouterr().out
    assert main(["-C", v, "research", "question", "new", Q, "--target", "nephropathy Z", "--source", "KRN1"]) == 0
    qid = capsys.readouterr().out.split()[0]
    assert main(["-C", v, "research", "narrate", qid, "--no-llm"]) == 0
    assert main(["-C", v, "research", "review", "queue", "--json"]) == 0
    capsys.readouterr()
    assert main(["-C", v, "research", "framework", "--impact", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["claims_needing_reextraction"] == 0
    assert main(["-C", v, "organize"]) == 0
    assert "Research workspace" in (ws.cfg.out_path / "INDEX.md").read_text()


def test_framework_version_change_impact(ws):
    extracted(ws)
    fw_path = ws.cfg.root.parent / "frameworks/disease-mechanism/framework.toml"
    fw_path.write_text(fw_path.read_text().replace("version = 2", "version = 3"))
    w2 = Workspace(ws.cfg, ws.store)
    imp = w2.framework_impact()
    assert imp["framework"] == "disease-mechanism@3" and imp["claims_needing_reextraction"] == 19
    assert len(Extractor(ws.cfg, ws.store, None, w2.fw).pending()) == 14  # every note, README included
