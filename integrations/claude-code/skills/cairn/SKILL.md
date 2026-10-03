---
name: cairn
description: Navigate, search and connect a folder of markdown notes organised by cairn. Use when the user asks about the contents of their notes, wants to find related notes, trace how ideas connect, answer a question from their notes, or derive insights across them.
---

# Navigating a cairn-organised notes folder

The notes folder has a generated navigation layer in `_cairn/` (configurable in `cairn.toml`):
`INDEX.md` (topics, hubs, bridges), `topics/*.md` (one map per cluster), `maintenance.md`,
and `AGENTS.md`. Human notes are the source of truth; the navigation layer is regenerated.

## Workflow

1. Get the map: `cairn overview` (or read `_cairn/INDEX.md`).
2. Narrow down: `cairn topic <topic-id>` for a cluster, `cairn search "<terms>"` for keywords.
   Search is BM25 over note sections, so it is much cheaper than grepping and reading files.
3. Read: `cairn show <note> [--section <heading>]`. Note ids are vault-relative paths
   without `.md`; titles, file names and aliases also resolve.
4. Expand: `cairn links <note>` lists outgoing links, backlinks, typed relations
   (supports / contradicts / extends / …) and similar notes not yet linked.
5. Connect: `cairn path <a> <b>` shows the chain of links/relations between two notes.

6. Research workspace (if `cairn.toml` sets `framework`):
   - `cairn research claim <id>` shows a claim with its exact passage, context, study and reviews.
   - `cairn research cases` lists tensions, conflicts, triangulations and missing bridges.
     `cairn research case <id>` shows one: premises, assumptions, competing explanations.
   - `cairn research path --to <target> [--from <source>]` gives reading paths. Each step is
     Supported, Assumed, Challenged or Missing.
   - `cairn research context <question-id>` is the evidence package to work from.
   Never upgrade a step's status in what you write: an Assumed or Missing step stays that way
   unless you cite a claim that changes it. Propose, don't accept: review decisions belong to the user.

Add `--json` to any command for structured output. Run commands from the notes folder or
pass `-C <folder>`.

## When answering from notes

- Cite notes as `[[note-id]]`. Separate what the notes say from what you infer.
- Say when the notes don't cover something rather than filling the gap from general knowledge.
- When you discover a relation worth keeping, record it instead of editing human notes:
  `cairn relate <src> <type> <dst> "<one-sentence reason>"`.
- Write derived notes only under `_cairn/insights/` or `_cairn/answers/`, citing sources.
- Never edit text between `<!-- cairn:start -->` and `<!-- cairn:end -->`.
- After adding or changing notes, run `cairn organize` to refresh the navigation layer.
