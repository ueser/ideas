"""Claims a good extraction would return for examples/disease-vault, used with a fake LLM."""

import json
from types import SimpleNamespace as NS


def C(s, ss, sc, kind, o, os_, oc, ev, ctx="", conf=0.9, quote=""):
    return {"subject": s, "subject_scale": ss, "subject_change": sc, "kind": kind, "object": o,
            "object_scale": os_, "object_change": oc, "evidence": ev, "context": ctx, "quote": quote,
            "confidence": conf}


CLAIMS = {
    "KRN1 kinase drives myofibroblast activation": {"note_scales": ["protein", "pathway", "cell_program"], "claims": [
        C("KRN1", "protein", "decrease", "causal", "myofibroblast activation", "cell_program", "decrease",
          "intervention", "primary human kidney fibroblasts", quote="siRNA knockdown of KRN1 reduced myofibroblast activation"),
        C("KRN1", "protein", "decrease", "causal", "SIG pathway", "pathway", "decrease", "intervention",
          "primary human kidney fibroblasts"),
        C("SIG pathway", "pathway", "decrease", "causal", "myofibroblast activation", "cell_program", "decrease",
          "intervention", "primary human kidney fibroblasts"),
    ]},
    "The KRN1–ADP2 complex": {"note_scales": ["protein_domain", "complex", "pathway"], "claims": [
        C("KRN1 kinase domain C-lobe", "protein_domain", "decrease", "causal", "KRN1-ADP2 complex", "complex",
          "decrease", "intervention", "HEK293"),
        C("KRN1-ADP2 complex", "complex", "decrease", "causal", "SIG signaling", "pathway", "decrease",
          "intervention", "HEK293"),
    ]},
    "Myofibroblasts and renal fibrosis": {"note_scales": ["cell_program", "cell_behavior", "tissue"], "claims": [
        C("myofibroblast activation", "cell_program", "decrease", "causal", "renal fibrosis", "tissue", "decrease",
          "intervention", "mouse"),
        C("SIG pathway", "pathway", "increase", "correlative", "ECM secretion", "cell_behavior", "increase",
          "observational", "fibroblasts", conf=0.6),
    ]},
    "Myofibroblast markers in nephropathy Z biopsies": {"note_scales": ["cell_program", "tissue", "system", "condition"],
                                                        "claims": [
        C("myofibroblast activation", "cell_program", "increase", "correlative", "Nephropathy Z", "condition",
          "increase", "observational", "patient biopsies"),
        C("renal fibrosis", "tissue", "increase", "correlative", "glomerular filtration", "system", "decrease",
          "observational", "patients"),
    ]},
    "Loss-of-function KRN1 variants in nephropathy Z": {"note_scales": ["gene", "condition"], "claims": [
        C("KRN1", "gene", "decrease", "correlative", "nephropathy Z", "condition", "increase", "genetic",
          "exome sequencing cohort", quote="loss-of-function variants in KRN1 enriched in nephropathy Z"),
    ]},
    "KRN1 in kidney macrophages": {"note_scales": ["protein", "cell_program", "tissue"], "claims": [
        C("KRN1", "protein", "decrease", "causal", "inflammatory macrophage program", "cell_program", "increase",
          "intervention", "macrophages (myeloid knockout mice)"),
        C("inflammatory macrophage program", "cell_program", "increase", "causal", "renal fibrosis", "tissue",
          "increase", "intervention", "mouse", conf=0.7),
    ]},
    "SIG inhibition in a fibrosis model": {"note_scales": ["pathway", "tissue", "system"], "claims": [
        C("SIG pathway", "pathway", "decrease", "causal", "renal fibrosis", "tissue", "decrease", "intervention", "UUO mouse"),
        C("SIG pathway", "pathway", "decrease", "causal", "glomerular filtration", "system", "increase",
          "intervention", "UUO mouse"),
    ]},
    "Notes from a review on GFR decline": {"note_scales": ["tissue", "system", "condition"], "claims": [
        C("renal fibrosis", "tissue", "increase", "causal", "glomerular filtration", "system", "decrease", "stated"),
        C("glomerular filtration", "system", "decrease", "causal", "nephropathy Z", "condition", "increase", "stated"),
    ]},
}


def responder(kw, n):
    system = kw.get("system", "")
    prompt = kw["messages"][0]["content"] if isinstance(kw["messages"][0]["content"], str) else ""
    if "You extract claims" in system:
        title = prompt.split("Note: ", 1)[1].split(" (", 1)[0]
        out = CLAIMS.get(title, {"note_scales": [], "claims": []})
    elif "You merge synonymous" in system:
        groups = [{"canonical": "SIG pathway", "members": ["SIG pathway", "SIG signaling"]}] \
            if "SIG signaling" in prompt else []
        out = {"groups": groups}
    else:
        raise AssertionError(f"unexpected call: {system[:60]}")
    return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(out))])
