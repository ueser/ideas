# cairn

An agentic harness that organises a folder of markdown notes so that **humans and AI agents**
can navigate it, find relevant information, see how ideas connect, and derive new insights.

Point it at any folder of `.md` files (Obsidian vault, Zettelkasten, docs folder, research notes).
It builds a knowledge graph of the notes and writes a navigation layer back into the folder as
plain markdown. Claude-powered passes then enrich the graph and work over it. For research questions, a
**research workspace** turns the notes into evidence-linked claims, which are read through a
framework you supply (for example, the scales of a disease mechanism). It then raises reasoning
cases (tensions, conflicts, gaps), builds competing narratives with distinguishing predictions,
and puts everything through human review. Your own writing is never rewritten.

```
notes/                         notes/_cairn/
  learning/spaced-rep.md   ─►    INDEX.md          topics, hubs, bridges, recent, insights
  sleep/caffeine.md              AGENTS.md         how an agent should navigate this folder
  work/deep-work.md              topics/*.md       a map of each cluster of related notes
  ...                            maintenance.md    broken links, orphans, duplicates, contradictions
                                 insights/*.md     cited insight notes written by the agent
                                 answers/*.md      saved answers to questions
```

## How it works

cairn has three layers, and each layer works without the ones above it.

**1. Structure (deterministic, offline, no dependencies)** — `cairn organize`
- Parses frontmatter, `[[wikilinks]]` (aliases, headings, embeds), relative markdown links,
  `#tags` and section structure. Resolves links the way Obsidian does: by path, file name,
  title or alias.
- Indexes every section in SQLite FTS5 (BM25), so agents can search instead of reading every file.
- Computes TF-IDF similarity between notes, PageRank over links ("hubs"), Louvain communities
  over links + relations + similarity ("topics"), and participation scores for notes that
  connect separate topics ("bridges").
- Renders the `_cairn/` navigation notes. With `--note-blocks` it also adds a managed
  *Connections* block (topic, backlinks, relations, related notes) to the end of each note.
  The block sits between `<!-- cairn:start -->` / `<!-- cairn:end -->` markers and is
  excluded from analysis, so generated links never feed back into the graph.

**2. Knowledge (Claude, incremental)** — `cairn run`
- `enrich`: per note, a summary, subject topics, note type, atomic key claims, open questions
  and entities. Results are cached by content hash, so only changed notes are reprocessed.
- `link`: for each note's nearest *unlinked* neighbours, Claude decides the typed relation:
  `supports`, `contradicts`, `extends`, `example_of`, `part_of`, `prerequisite_for`,
  `alternative_to`, `caused_by` or `same_topic`, with a one-sentence reason. Superficial overlap is
  rejected. Each note is re-checked only when its content or candidate set changes.
- `name_topics`: gives each cluster a human name, a synopsis of what its notes collectively
  establish, and the questions they leave open.
- Enrichment sharpens similarity, and relations reshape the topics. The knowledge layer is kept
  outside your notes, in `.cairn/index.db`, and follows a note when it is renamed or moved.

**3. Reasoning (Claude agents with navigation tools)**
- `cairn ask "…"`: an agent answers from the notes. It searches, reads, follows links and traces
  paths, then cites notes as `[[note-id]]` and states gaps. `--save` files the answer under
  `_cairn/answers/`.
- `cairn insights`: an agent is seeded with structural signals: unlinked cross-topic pairs,
  contradictions, bridge notes and open questions. It explores and writes cited insight notes
  (connections, tensions, patterns, conclusions, gaps) to `_cairn/insights/`. It also records the
  relations it establishes.

Humans, the built-in agent and external agents all use **one toolset**: `overview`, `search`,
`read_note`, `note_links`, `topic`, `find_path`, `list_notes`, `maintenance_report`, plus
`add_relation` / `write_note` when writes are allowed. That toolset is available as a CLI, as an
MCP server and as a Claude Code skill.

## Research workspace: frameworks as lenses over evidence

For research questions, cairn builds an inspectable workspace beside the notes. The basic unit is
a **claim linked to its source passage**. A *framework* that you supply decides how claims are
organised and which reasoning checks are allowed to run. Every interpretation can be traced back
to a passage, and every conclusion goes through human review.

```
original notes ─▶ passages ─▶ contextual claims ─▶ framework mappings ─▶ cases & narratives ─▶ review
   (yours)        (anchored)    (JSONL records)      (state → scale)       (Markdown records)
```

The workspace has four layers. Each layer is derived from the one before it and never the
other way round, so you can revise a framework without rewriting your evidence.

| Layer | What it holds | Where |
|---|---|---|
| Sources | what the notes say; never modified | your notes |
| Claims | interpretations of specific passages, with context and attribution | `_research/records/claims/<note>.jsonl` |
| Mappings | how entity states fit the framework's scales (many-to-many, or *unmapped*) | `_research/records/mappings/<framework>.jsonl` |
| Reasoning | cases, questions, competing narratives, predictions | `_research/cases/`, `questions/`, `narratives/` (Markdown) |

Generated views live in `_research/views/`: an index, a **framework map** of scale lanes, an
**entity card** per entity, and the **review queue**. The SQLite index is only a cache. Every
other file under `_research/` is the canonical record, so you can diff it and commit it to git.

### Frameworks are versioned analysis contracts

A framework has a typed definition and a readable guide; see
[`examples/frameworks/disease-mechanism/`](examples/frameworks/disease-mechanism). The definition
declares:
- **scales** (gene → protein domain → … → organism condition);
- **relation types**, each with a kind (causal, associative, compositional, descriptive) and a sign;
- **context fields** every claim records (species, cell type, tissue, intervention, time, assay);
- which fields must **match** before two claims are compared;
- **evidence types**, and which of them can justify a causal reading;
- the **questions** the framework is for, and the **checks** allowed to run.

The guide is given to the extractor but never executed as rules. Analyses record the framework
version they used, and `cairn research framework --impact` shows what a new version touches.

```toml
# cairn.toml in your notes folder
framework = "frameworks/disease-mechanism/framework.toml"
```

### Claims are about states, in context

The system doesn't record "A connects to X". It records *increased activity of A increases X,
in context C, according to evidence E.* Each claim stores:
- the subject and object **states**: entity, property, direction, and the effect on the
  entity's activity, marked as *stated* or *inferred*;
- the relation type, any **conditions** ("only when B"), negation and qualifiers;
- the claim type (observation, association, causal, hypothesis) and the attribution
  (publication, note author, cited work);
- the context, the evidence type and the **study** (DOI or PMID, or citation);
- the exact **passage** with line numbers, heading and a hash of the note revision.

A claim counts as causal only if it is worded causally *and* its evidence type can support
causation. Otherwise it is read as an association. Several notes about one study are one source
of evidence.

### Checks produce cases, not scores

| Check | Case |
|---|---|
| `conditional_sign_tension` | A loop whose signs multiply to `−`. Your A→X, X in W, A↓~W example becomes a **potential mechanistic tension**. The case shows the challenging claim, the route it challenges, the **missing premise** (X is only *observed in* W, so does X contribute to W or respond to it?), the context and property assumptions (the variant is *inferred* to be loss-of-function), and **competing explanations**, each with the evidence that would tell them apart. When every link is causal, the loop is reported as **opposing causal routes**, which may both be real (an incoherent feed-forward arrangement). |
| `conflicting_result` | Opposite or null results for the same pair from different studies, with the context differences between them. |
| `independent_evidence` | Positive triangulation. The case names which claim is strengthened, by which *independent studies*, and under which assumptions. Repeated reports of one study count once, and bare statements don't count. |
| `missing_bridge` | A top-scale entity, such as a disease, connected only by association. |

A closed loop never changes a confidence value. Cases that raise the same issue are merged before
they reach the review queue.

### Questions, competing narratives, predictions

```bash
cairn research question new "Which paths connect a KRN1 perturbation to nephropathy Z?" \
      --target "nephropathy Z" --source KRN1
cairn research path --to "nephropathy Z" --from KRN1      # candidate reading paths
cairn research narrate q-which-paths-…                    # A vs B (+ --no-llm for skeletons only)
```

A narrative is one linear **reading path** chosen from a graph that can branch, loop and skip
scales. Each step is marked **Supported** (a causal claim in that direction), **Assumed**
(association or hypothesis), **Challenged** (a conflicting result on that link) or **Missing**
(no claim). Gaps stay visible. Scales a path doesn't use are listed as possibly irrelevant, not
counted as gaps.

Tensions that touch a narrative are listed as challenges to it. The comparison note gives
**distinguishing predictions**:
- the source's net effect on the target under A and under B;
- mediation tests that block an intermediate only one narrative needs.

When Claude writes the prose, every citation and step status is checked mechanically. Unknown
claims are removed, unsupported "Supported" steps are downgraded, and unconnected steps become
Missing. The corrections are recorded in the narrative.

### Review, staleness, agents

```bash
cairn research review queue                                   # grouped by note, ⚠ = check first
cairn research review accept --note papers/chen-2019 -m "checked against passages"
cairn research review revise c-0123456789 --set subject.effect_basis=stated -m "paper shows LoF"
cairn research review reject potential-tension-… -m "different disease subtype"
cairn research claim c-0123456789                              # evidence inspector
```

Accepting a claim records a review decision, not scientific truth. Decisions go into an
append-only log (`records/reviews.jsonl`). Revising a claim creates a new claim that supersedes
the old one.

Staleness is handled by `cairn research sync` (run automatically by the other commands):
- **Edited note:** if a claim's passage still exists, the claim is re-anchored and keeps its
  status. If the passage changed, the claim becomes *stale*.
- **Renamed note:** the claims follow it.
- **Deleted note:** its claims become *source_missing*.
- **Dependent records:** cases and narratives built on claims that are no longer live go back to
  review. Reviewer notes written below the marker in a case or narrative survive regeneration.

`cairn research investigate <question>` runs a **bounded agent**. Its scope is one question, the
current state of the evidence, the framework version and a turn budget. It reads with the same
tools you use and can only *propose* cases, which go into the review queue. The evidence package
it starts from is shown by `cairn research context <question>`. That package holds:
- the question and the candidate narratives;
- the claims with their passages, plus counterevidence;
- the relevant cases, gaps and assumptions;
- exclusions (claims that aren't live, unmapped entities) and a token estimate.

### Evaluation

[`examples/eval-vault`](examples/eval-vault) is a synthetic collection built for the pilot
evaluation. It contains:
- one study reported in two notes;
- negations, and conflicting results across species;
- notes with unknown context;
- a missing causal bridge and the A/X/W tension;
- a genuinely compatible cross-scale link supported by three labs.

It comes with gold claims and the findings an expert should reach.

```bash
cairn -C examples/eval-vault research run          # needs an API key
cairn -C examples/eval-vault research eval --gold examples/eval-vault/gold
```

The scorer reports:
- claim precision and recall, keyed by note, entities, sign and negation;
- context accuracy, including context the extractor **invented**;
- passage anchoring;
- whether the expected cases were found;
- study independence, negation handling, and whether unknown context stayed unknown.

The test suite also covers a renamed note and an edited source. Producing more claims or cases
doesn't improve the score.

## Quick start

```bash
pip install -e '.[all]'          # Python ≥ 3.11; the core needs no dependencies
cd ~/notes
cairn init                       # cairn.toml, .gitignore entry, first index + _cairn/
cairn overview                   # topics, hubs, bridges, recent, loose notes
cairn search "sleep memory"      # BM25 over sections, with snippets
cairn links spaced-repetition    # links, backlinks, relations, unlinked similar notes
cairn path caffeine deep-work    # how two ideas connect, hop by hop
cairn doctor                     # broken links, orphans, duplicates, contradictions

export ANTHROPIC_API_KEY=...     # or `ant auth login`
cairn run                        # index → enrich → link → name topics → render
cairn ask "How should I schedule study sessions?" --save
cairn insights -n 3 --focus "learning and sleep"
```

Every command accepts `--json`. Read commands re-index automatically when notes have changed.
Try it on the bundled sample: `cairn -C examples/vault organize`.

### Use it from other agents

- **MCP** (Claude Code, Claude Desktop, any MCP client): `cairn -C ~/notes mcp` serves the
  read-only tools over stdio. Add `--allow-writes` to expose `add_relation` and `write_note`.
  See [`integrations/mcp.json.example`](integrations/mcp.json.example).
  In Claude Code: `claude mcp add notes -- cairn -C ~/notes mcp`.
- **Claude Code skill**: copy [`integrations/claude-code/skills/cairn`](integrations/claude-code/skills/cairn)
  into `~/.claude/skills/` or into `<notes>/.claude/skills/`. Claude Code will then use the CLI to
  navigate.
- **Any agent with file access**: `_cairn/AGENTS.md` explains the layout and conventions,
  and `INDEX.md` is the entry point.

## Configuration

An optional `cairn.toml` at the vault root:

```toml
output_dir = "_cairn"        # generated navigation notes
link_style = "wiki"          # or "markdown" for GitHub-style relative links
write_note_blocks = false    # managed Connections block at the end of each note
exclude = ["templates/*"]    # extra globs to skip (.git, .obsidian, .cairn are always skipped)
related_k = 6                # similar notes per note (also the link-pass candidate count)
min_similarity = 0.12
model = "claude-opus-5-5"
effort = "medium"            # bulk passes
agent_effort = "high"        # ask / insights
max_workers = 4              # parallel Claude calls in bulk passes
framework = ""               # path to a framework TOML; enables `cairn research`
research_dir = "_research"   # claims, mappings, cases, narratives, questions, views
```

## Design principles

- **Your notes stay yours.** cairn writes only to `_cairn/` and `_research/`, to the marked block
  when you opt in, and to `.cairn/` (the index, which you can rebuild). Commit `_research/`: it is
  the canonical record of interpretations and review decisions. The agents record what they learn as typed
  relations and cited derived notes. They do not edit your prose.
- **Deterministic first, LLM second.** Structure, search and graph metrics are reproducible and
  free. Claude is used where judgement matters: summaries, relation types, topic synthesis and
  reasoning. The LLM passes are incremental and cached.
- **Everything is grounded.** Relations carry reasons, and insights and answers cite source notes.
  Generated notes are searchable but kept out of the graph analysis, so conclusions cannot
  reinforce themselves.
- **Plain files.** The navigation layer is markdown with standard links. It works in Obsidian,
  on GitHub, in an editor, or with `cat`.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Layout: `vault.py` (parsing, link resolution) · `store.py` (SQLite schema and queries) ·
`indexer.py` (index pipeline) · `similarity.py` / `graph.py` (TF-IDF, PageRank, Louvain, paths) ·
`render.py` (navigation layer) · `tools.py` (shared toolset) · `agent.py` (Claude passes and agent
loop) · `mcp_server.py` · `cli.py`. The research workspace is in `research/`: `framework.py`
(versioned contracts) · `records.py` (canonical files, passages, sync/staleness, review log) ·
`extract.py` (claim and identity proposals) · `model.py` (state signs, causal reading) ·
`checks.py` (cases) · `narratives.py` (paths, statuses, predictions, evidence package,
validation) · `workspace.py` (pipeline and the single write path) · `views.py` · `evaluate.py` · `cli.py`.

Claude calls use the Anthropic Python SDK with server-side refusal fallbacks enabled
(`fallbacks="default"`), structured outputs for the bulk passes, and prompt caching in the
agent loop.
