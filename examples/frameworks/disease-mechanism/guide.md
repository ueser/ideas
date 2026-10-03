# Guide: multi-scale disease mechanism

This framework reads notes as evidence about **how a molecular change becomes a disease**.
Biology is organised in scales with emergent concepts at each level: gene, protein domain,
protein, protein complex, signaling pathway, cellular program, cell behaviour, tissue
behaviour, system behaviour, and organism condition.

## What a claim should capture

Use states, not bare entities. "Protein A connects to X" is too vague to reason with.
Write it as: *increased activity of A increases X, in context C, according to evidence E.*

- **Entity and property.** KRN1 *activity*, KRN1 *loss-of-function variant*, myofibroblast
  *activation*, nephropathy Z *presence*.
- **Direction.** Record what was observed: increase, decrease, present, absent.
- **Effect on activity.** Say whether the state raises or lowers the entity's activity, and
  whether the note states this or you are inferring it. Calling a variant "inactivating"
  without functional data is an inference.
- **Claim type.** *Causal* needs intervention or genetic evidence in this note.
  Co-occurrence is an *association*. A measured finding is an *observation*. Speculation
  is a *hypothesis*.
- **Context.** Species, cell type, tissue, intervention, time, assay. Leave a field empty
  rather than guess. Many apparent contradictions turn out to be context differences.
- **Study.** Several notes about one study are a single source of evidence.

## How the scales are used

The scales organise the evidence. They are not a ladder every explanation must climb:
- a step may stay within one scale, or skip scales;
- feedback is allowed when it has time context;
- a scale can be irrelevant to a particular question;
- material that fits no scale stays *unmapped* and remains searchable.

## Examples

- "siRNA knockdown of KRN1 reduced myofibroblast activation in human kidney fibroblasts."
  KRN1 activity ↓ —activates→ myofibroblast activation ↓. Causal, intervention. Context:
  human, kidney fibroblasts.
- "Loss-of-function variants in KRN1 are enriched in nephropathy Z."
  KRN1 loss-of-function variant present (activity ↓, inferred) —associated_with→
  nephropathy Z presence ↑. Association, genetic evidence. Context: human cohort.
