# cairn

An agentic harness that organises a folder of markdown notes so that **humans and AI agents**
can navigate it, find relevant information, see how ideas connect, and derive new insights.

Point it at any folder of `.md` files (Obsidian vault, Zettelkasten, docs folder, research notes).
It builds a knowledge graph of the notes and writes a navigation layer back into the folder as
plain markdown. Claude-powered passes then enrich the graph and work over it. Optionally, a **framework**
you supply (for example, the scales of a disease mechanism) is used to read the notes as claims,
check them for consistency, and assemble them into testable narratives. Your own writing is never
rewritten.

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

## Framework lenses: reading notes through your own model

Topics and links organise notes by what they say. A **framework** organises them by what they
mean for a question you care about. You describe the framework in a TOML file:
- ordered **scales** (levels of explanation);
- the **evidence types** that count, and how much each weighs;
- free-text **guidance**, which is passed to Claude as written.

cairn then reads every note through that lens. The bundled example,
[`examples/frameworks/disease-mechanism.toml`](examples/frameworks/disease-mechanism.toml),
explains a disease across ten scales: gene → protein domain → protein → complex →
signaling pathway → cellular program → cell behaviour → tissue → system → organism condition.

```toml
# cairn.toml in your notes folder
framework = "frameworks/disease-mechanism.toml"
```

**What happens**

1. **Claim extraction (Claude).** Each note becomes signed claims. A claim names a subject and an
   object, each placed at one of your scales, and says how a change in one goes with a change in the
   other, whether the link is causal or correlative, the evidence type, the context (cell type,
   model, cohort) and a supporting quote. "Inactivating mutations in A are found in W" becomes
   *A ↓ ~ W ↑* (correlative, genetic). The output schema is generated from your framework.
   Extraction is cached by note content and framework version.
2. **Entity normalisation (Claude).** Synonyms are merged within each scale (e.g. "SIG signaling"
   and "SIG pathway"). Case, punctuation and Greek-letter variants are merged without a model.
3. **Multi-scale graph.** Claims are aggregated per pair of entities. Independent notes accumulate
   evidence (noisy-OR), repeats within one note don't, and opposing signs mark the link as
   *conflicting*.
4. **Triangulation.** Every loop of up to 4 links is checked for **sign balance**: the product of
   its signs must be `+`. Your example, *A ↑ → X ↑*, *X ↑ ~ W ↑*, *A ↓ ~ W ↑*, multiplies to `−`,
   so the loop is incoherent. Links in coherent loops gain confidence and links in incoherent loops
   lose it. Links that sit in more incoherent than coherent loops are ranked as **suspects**, with
   their contexts, because contradictions are often context differences or missing mediators.
5. **Chains.** A beam search finds linear chains that climb the scales and end at a target (by
   default, the best-connected entity at the top scale). Chains are ranked by scales covered,
   confidence, skipped scales and incoherence. Every step lists *open points*: correlative-only
   links (perturb and measure), skipped scales (find the mediator), conflicting or incoherent
   links, and weak evidence.
6. **Narrative (Claude agent).** `cairn mech narrate` gives an agent the ranked chains and the
   incoherent loops, along with the mechanism tools. It checks the key steps against the source
   notes, looks for missing mediators, and writes `_cairn/mechanisms/…`. The note contains a
   summary, one linear narrative with each step cited and graded, a Mermaid diagram, the
   inconsistencies, and **testable hypotheses**, each with an experiment, a predicted outcome
   and what would falsify it.

```bash
cairn mech extract                 # claims + synonym merge + render (incremental)
cairn mech overview                # entities per scale, loop consistency, chain targets
cairn mech chains --to "disease W" # ranked chains up the scales, with open points
cairn mech check                   # incoherent loops, suspect links, conflicting links
cairn mech entity "protein A"      # causes, effects, associations, quotes, loops
cairn mech narrate --to "disease W" [--from "gene A"]
```

The lens is also rendered as browsable notes under `_cairn/<framework>/`:
- an index with a scale table and the best chains as Mermaid diagrams;
- one page per scale, linking up and down the hierarchy;
- an **entity card** per entity, with upstream causes, downstream effects, associations,
  evidence quotes and source notes;
- `chains.md` and `consistency.md`.

With `--note-blocks`, each paper note links to the entities it contributes. Agents get the same
lens through the `mechanism_overview`, `mechanism_entity`, `mechanism_chains` and
`mechanism_consistency` tools (CLI, MCP and the built-in agents).

Try it on [`examples/disease-vault`](examples/disease-vault): eight **synthetic** paper notes about
a fictional nephropathy, with a built-in genetic-vs-mechanistic contradiction. Run
`cairn -C examples/disease-vault mech extract`; this needs an API key.

The framework format is generic. Any domain with levels of explanation and directional claims
fits: economics (policy → market → firm → household), ecology, or software systems.

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
framework = ""               # path to a framework TOML; enables `cairn mech` and the lens pages
```

## Design principles

- **Your notes stay yours.** cairn writes only to `_cairn/`, to the marked block when you opt in,
  and to `.cairn/` (the index, which you can rebuild). The agents record what they learn as typed
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
loop) · `framework.py` (framework files) · `mechanism.py` (signed multi-scale graph, triangulation,
chains) · `mech_agent.py` (claim extraction, normalisation, narrative agent) · `mechanism_render.py` ·
`mcp_server.py` · `cli.py`.

Claude calls use the Anthropic Python SDK with server-side refusal fallbacks enabled
(`fallbacks="default"`), structured outputs for the bulk passes, and prompt caching in the
agent loop.
