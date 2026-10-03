# Evaluation collection (synthetic)

These notes are **fictional**: KRN1, ADP2, the SIG pathway and nephropathy Z do not exist. The
collection is built to exercise the research workspace on the situations from the pilot plan:

| Situation | Where |
|---|---|
| One study reported in two notes | `lee-2021-fibroblast`, `lee-2021-from-review` (same DOI) |
| Negation | `garcia-2023-rat`, `tanaka-2022-fibroblast` ("did not …") |
| Conflicting results across species | `park-2022-inhibitor` (mouse) vs `garcia-2023-rat` (rat) |
| Mixed species / unknown context | mouse, rat, human notes; `inbox/unknown-context`, `review-gfr` |
| Missing causal bridge | myofibroblast activation is only *observed in* nephropathy Z |
| Potential tension (A → X, X in W, A↓ ~ W) | `lee-2021-fibroblast`, `chen-2019-biopsies`, `nakamura-2023-exome` |
| Genuinely compatible cross-scale link | SIG pathway → myofibroblast activation in three labs |
| Renamed note, edited source | exercised in `tests/test_research.py` |

`gold/claims.json` holds the claims an expert would extract (in the extractor's output format).
`gold/expected.json` lists the cases a reviewer should see. Score a real extraction with:

    cairn -C examples/eval-vault research run
    cairn -C examples/eval-vault research eval --gold examples/eval-vault/gold
