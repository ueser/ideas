"""Claude-powered passes over the vault.

Bulk passes (one structured call per item, run in parallel, cached by content hash):
  enrich       summary, topics, type, key claims, open questions, entities per note
  link         typed relations between a note and its nearest unlinked neighbours
  name_topics  a human name, synopsis and open questions for each topic cluster

Agent loops (Claude navigates with the same tools humans and MCP clients use):
  ask          answer a question from the notes, with citations
  insights     explore across topics and write cited insight notes
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .config import Config
from .store import RELATION_TYPES, Store
from .tools import ToolError, VaultTools, call_tool, tool_definitions

MAX_NOTE_CHARS = 60_000
MAX_TOOL_RESULT_CHARS = 20_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    pass


ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "1-2 sentences: what this note says, not what it is about"},
        "topics": {"type": "array", "items": {"type": "string"}, "description": "3-7 lowercase subject phrases"},
        "note_type": {"type": "string", "enum": ["concept", "idea", "project", "meeting", "log", "reference",
                                                  "howto", "question", "person", "source", "other"]},
        "key_claims": {"type": "array", "items": {"type": "string"},
                       "description": "0-5 atomic, self-contained claims or decisions the note asserts"},
        "open_questions": {"type": "array", "items": {"type": "string"}, "description": "0-3 unresolved questions"},
        "entities": {"type": "array", "items": {"type": "string"}, "description": "named people, tools, orgs, works"},
    },
    "required": ["summary", "topics", "note_type", "key_claims", "open_questions", "entities"],
    "additionalProperties": False,
}

LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "relations": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "candidate": {"type": "string"},
                "type": {"type": "string", "enum": RELATION_TYPES + ["none"]},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["candidate", "type", "reason", "confidence"],
            "additionalProperties": False,
        }},
    },
    "required": ["relations"],
    "additionalProperties": False,
}

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "2-5 word topic name"},
        "synopsis": {"type": "string", "description": "2-4 sentences: what these notes collectively establish"},
        "open_questions": {"type": "array", "items": {"type": "string"}, "description": "0-4 questions the notes leave open"},
    },
    "required": ["name", "synopsis", "open_questions"],
    "additionalProperties": False,
}

ENRICH_SYSTEM = """You index personal and team knowledge notes so that people and AI agents can find and \
connect them later. Describe the note faithfully: summarise what it actually claims or records, extract \
claims as standalone sentences that make sense without the note, and do not invent content."""

LINK_SYSTEM = f"""You decide how notes in a knowledge base relate. For each candidate, pick the single \
relation that best describes SOURCE → CANDIDATE, or "none" when the overlap is superficial (shared \
vocabulary, same broad field). Relations: {", ".join(RELATION_TYPES)}. "same_topic" is the weakest; prefer a \
more specific relation when the notes support one. The reason must cite the specific idea that connects them, \
in one sentence. Confidence is 0-1."""

TOPIC_SYSTEM = """You name and summarise clusters of related notes in a knowledge base. The name should be \
what a person would call this area of their thinking. The synopsis should state what the notes collectively \
establish or argue, not list them."""

AGENT_SYSTEM = """You are a research agent working over a collection of markdown notes. You navigate with \
tools: overview (map of topics), search (BM25 full text), read_note, note_links (links, backlinks, typed \
relations, similar notes), topic (cluster map), find_path (how two notes connect), list_notes. When a \
framework is configured, the mechanism_* tools expose a multi-scale model of claims extracted from the \
notes, with signs, evidence strength and loop consistency.

Work like a careful researcher: start broad, follow links and relations, read the notes that matter in full, \
and look for evidence on both sides. Ground every statement in the notes and cite them inline as [[note-id]]. \
Distinguish what the notes state from what you infer, and say plainly when the notes do not cover something."""

ASK_TASK = """Question: {question}

Answer from the notes. Finish with a concise answer (markdown) that cites notes as [[note-id]], then a short \
"Gaps" line if the notes leave part of the question open.{save}

Collection overview:
{overview}"""

INSIGHTS_TASK = """Explore this collection and derive up to {n} insights that are not written down in any \
single note: connections between separate topics, tensions or contradictions, patterns repeated across notes, \
conclusions that follow from combining notes, or important gaps.{focus}

For each insight worth keeping, call write_note with kind="insights": a clear title stating the insight, a \
body that explains the reasoning step by step citing notes as [[note-id]], and the list of source note ids. \
When you establish a specific relation between two notes, record it with add_relation. Quality over \
quantity: skip anything obvious or already stated in a note. End with a short summary of what you wrote.

Signals worth examining:
{signals}

Collection overview:
{overview}"""


def _echoable(content) -> list:
    """Blocks to send back after a server-side fallback: before the last fallback marker only text survives."""
    idx = max((i for i, b in enumerate(content) if b.type == "fallback"), default=None)
    if idx is None:
        return list(content)
    return [b for b in content[:idx] if b.type == "text"] + list(content[idx + 1:])


class Claude:
    def __init__(self, cfg: Config, client=None):
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise LLMError("The agent features need the anthropic package: pip install 'cairn-notes[ai]'") from e
            client = anthropic.Anthropic()
        self.client = client
        self.cfg = cfg

    def _create(self, **kw):
        resp = self.client.beta.messages.create(
            model=self.cfg.model, betas=[FALLBACK_BETA], fallbacks="default", **kw)
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        return resp

    def structured(self, system: str, prompt: str, schema: dict, effort: str | None = None) -> dict:
        resp = self._create(
            max_tokens=16000, system=system, messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort or self.cfg.effort, "format": {"type": "json_schema", "schema": schema}})
        if resp.stop_reason == "max_tokens":
            raise LLMError("response was cut off at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text in response")
        return json.loads(text)

    def run_agent(self, task: str, tools: VaultTools, effort: str | None = None, max_turns: int = 40,
                  on_event: Callable[[str], None] | None = None) -> str:
        defs = tool_definitions(tools)
        messages: list = [{"role": "user", "content": task}]
        for turn in range(max_turns):
            last = turn == max_turns - 1
            resp = self._create(
                max_tokens=16000, system=AGENT_SYSTEM, tools=defs, messages=messages,
                tool_choice={"type": "none"} if last else {"type": "auto"},
                output_config={"effort": effort or self.cfg.agent_effort}, cache_control={"type": "ephemeral"})
            content = _echoable(resp.content)
            messages.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.type == "tool_use"]
            if not uses:
                if resp.stop_reason == "max_tokens":
                    messages.append({"role": "user", "content": "Continue."})
                    continue
                return "\n".join(b.text for b in content if b.type == "text").strip()
            results = []
            for u in uses:
                if on_event:
                    on_event(f"{u.name}({', '.join(f'{k}={v!r}' for k, v in u.input.items())})")
                try:
                    out = json.dumps(call_tool(tools, u.name, dict(u.input)), ensure_ascii=False, default=str)
                    if len(out) > MAX_TOOL_RESULT_CHARS:
                        out = out[:MAX_TOOL_RESULT_CHARS] + " …[truncated; narrow the request]"
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
                except (ToolError, TypeError) as e:
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": str(e), "is_error": True})
            if turn == max_turns - 2:
                results.append({"type": "text", "text": "Turn budget reached. Give your final answer now."})
            messages.append({"role": "user", "content": results})
        return ""


def _parallel(items: list, fn: Callable, workers: int, label: str, log: Callable[[str], None]):
    """Run fn(item) in a thread pool and yield (item, result) on the calling thread."""
    if not items:
        return

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for i, fut in enumerate(as_completed(futures), 1):
            item = futures[fut]
            try:
                result = fut.result()
            except Exception as e:  # keep going; one bad item shouldn't stop the pass
                log(f"[{label} {i}/{len(items)}] FAILED {item}: {e}")
                continue
            log(f"[{label} {i}/{len(items)}] {item}")
            yield item, result


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


class Organizer:
    def __init__(self, cfg: Config, store: Store, llm: Claude, log: Callable[[str], None] = _stderr):
        self.cfg, self.store, self.llm, self.log = cfg, store, llm, log

    def _run(self, label: str, system: str, schema: dict, prompts: dict[str, str]):
        """Bulk structured calls. Prompts are built on the calling thread; workers only hit the API."""
        def work(key: str) -> dict:
            return self.llm.structured(system, prompts[key], schema)
        return _parallel(list(prompts), work, self.cfg.max_workers, label, self.log)

    # ---- enrich ---------------------------------------------------------
    def enrich(self, force: bool = False, limit: int | None = None) -> int:
        todo = [n for n in self.store.notes()
                if force or self.store.enrichment_hash(n["id"]) != n["hash"]][: limit or None]
        prompts, hashes = {}, {}
        for n in todo:
            body, note = n["body"], ""
            if len(body) > MAX_NOTE_CHARS:
                note = f"\n(Note is long; showing the first {MAX_NOTE_CHARS} of {len(body)} characters.)"
                body = body[:MAX_NOTE_CHARS]
            prompts[n["id"]] = (f"Path: {n['path']}\nTitle: {n['title']}\nTags: {', '.join(n['tags']) or '-'}"
                                f"{note}\n\n<note>\n{body}\n</note>")
            hashes[n["id"]] = n["hash"]
        done = 0
        for nid, data in self._run("enrich", ENRICH_SYSTEM, ENRICH_SCHEMA, prompts):
            self.store.set_enrichment(nid, hashes[nid], data)
            done += 1
        return done

    # ---- link -----------------------------------------------------------
    def _candidates(self, nid: str) -> list[str]:
        linked = {l["dst"] for l in self.store.outlinks(nid)} | set(self.store.backlinks(nid))
        checked_from_other_side = {r["src"] for r in self.store.relations(nid)
                                   if r["dst"] == nid and r["source"] == "llm-link"}
        return [d for d, _ in self.store.similar(nid, self.cfg.related_k)
                if d not in linked and d not in checked_from_other_side]

    def _brief(self, nid: str, chars: int) -> str:
        n = self.store.note(nid)
        e = n["enrichment"] or {}
        claims = "; ".join(e.get("key_claims", [])[:5])
        text = f"id: {nid}\ntitle: {n['title']}\nsummary: {n['summary'] or '-'}\n"
        if claims:
            text += f"claims: {claims}\n"
        return text + f"excerpt: {n['body'][:chars]}\n"

    def link(self, force: bool = False, limit: int | None = None) -> int:
        todo = {}
        hashes = {}
        for n in self.store.notes():
            cands = self._candidates(n["id"])
            state = self.store.link_check_state(n["id"])
            if cands and (force or state is None or state != (n["hash"], sorted(cands))):
                todo[n["id"]] = cands
                hashes[n["id"]] = n["hash"]
        todo = dict(list(todo.items())[: limit or None])
        prompts = {nid: f"SOURCE\n{self._brief(nid, 4000)}\nCANDIDATES\n" +
                   "\n".join(f"---\n{self._brief(c, 1200)}" for c in cands) for nid, cands in todo.items()}
        added = 0
        for nid, data in self._run("link", LINK_SYSTEM, LINK_SCHEMA, prompts):
            cands = todo[nid]
            self.store.clear_relations(nid, "llm-link")
            for r in data["relations"]:
                if r["type"] == "none" or r["candidate"] not in cands or r["confidence"] < 0.5:
                    continue
                self.store.add_relation(nid, r["candidate"], r["type"], r["reason"], r["confidence"], "llm-link")
                added += 1
            self.store.set_link_checked(nid, hashes[nid], cands)
        return added

    # ---- topics ---------------------------------------------------------
    def name_topics(self, force: bool = False) -> int:
        prompts, members = {}, {}
        for t in self.store.topics():
            if not (force or t["stale"] or not t["synopsis"]):
                continue
            members[t["id"]] = self.store.topic_members(t["id"])
            lines = []
            for m in members[t["id"]][:40]:
                n = self.store.note(m)
                lines.append(f"- {n['title']}: {n['summary'] or n['body'][:300]}")
            more = f"\n(+{len(members[t['id']]) - 40} more notes)" if len(members[t["id"]]) > 40 else ""
            prompts[t["id"]] = (f"Key terms: {', '.join(t['terms'])}\n\nNotes in this cluster:\n"
                                + "\n".join(lines) + more)
        done = 0
        for tid, data in self._run("topic", TOPIC_SYSTEM, TOPIC_SCHEMA, prompts):
            self.store.set_topic_meta(tid, data["name"], data["synopsis"], data["open_questions"], members[tid])
            done += 1
        return done

    # ---- agents ---------------------------------------------------------
    def ask(self, question: str, save: bool = False, on_event=None) -> tuple[str, list[str]]:
        tools = VaultTools(self.cfg, self.store, allow_writes=save)
        save_hint = ("\n\nAfter answering, save the answer with write_note(kind=\"answers\") citing the notes "
                     "you relied on.") if save else ""
        task = ASK_TASK.format(question=question, save=save_hint,
                               overview=json.dumps(tools.overview(), ensure_ascii=False))
        answer = self.llm.run_agent(task, tools, on_event=on_event)
        return answer, tools.written

    def insights(self, n: int = 3, focus: str | None = None, on_event=None) -> tuple[str, list[str]]:
        tools = VaultTools(self.cfg, self.store, allow_writes=True)
        task = INSIGHTS_TASK.format(
            n=n, focus=f"\n\nFocus on: {focus}" if focus else "",
            signals=json.dumps(self.signals(), ensure_ascii=False),
            overview=json.dumps(tools.overview(), ensure_ascii=False))
        summary = self.llm.run_agent(task, tools, on_event=on_event, max_turns=60)
        return summary, tools.written

    def signals(self) -> dict:
        """Structural hints for where insights are likely: cross-topic pairs, tensions, open questions."""
        db = self.store.db
        cross = [dict(r) for r in db.execute(
            "SELECT s.src, s.dst, s.score FROM similar s JOIN notes a ON a.id=s.src JOIN notes b ON b.id=s.dst "
            "WHERE a.topic IS NOT b.topic AND s.src < s.dst "
            "AND NOT EXISTS (SELECT 1 FROM links l WHERE (l.src=s.src AND l.dst=s.dst) OR (l.src=s.dst AND l.dst=s.src)) "
            "ORDER BY s.score DESC LIMIT 12")]
        questions = []
        for n in self.store.notes():
            for q in (n["enrichment"] or {}).get("open_questions", [])[:2]:
                questions.append({"note": n["id"], "question": q})
        return {
            "unlinked_cross_topic_pairs": cross,
            "contradictions": [{k: r[k] for k in ("src", "dst", "reason")}
                               for r in self.store.relations() if r["type"] == "contradicts"],
            "bridges": [n["id"] for n in sorted(self.store.notes(), key=lambda n: -n["bridge"])[:6]
                        if n["bridge"] > 0.3],
            "open_questions": questions[:25],
        }
