"""Research workspace: evidence-linked claims, framework mappings, reasoning cases,
competing narratives and human review, stored as files beside the original notes.

Layers (each derived from the one before, never the reverse):
  sources    the original markdown notes (authoritative for what the notes say)
  claims     records/claims/<note>.jsonl, each claim anchored to a source passage
  mappings   records/mappings/<framework>.jsonl, how entity states fit the framework's scales
  reasoning  cases/*.md and narratives/**.md, pinned to claim ids and source revisions
"""
