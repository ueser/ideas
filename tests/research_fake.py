"""A stand-in for Claude that returns the gold extraction for each note."""

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

GOLD = json.loads((Path(__file__).resolve().parent.parent / "examples/eval-vault/gold/claims.json").read_text())


class Messages:
    def __init__(self, gold=GOLD, agent=None, synth=None):
        self.gold, self.agent, self.synth, self.calls = gold, agent, synth, []

    def create(self, **kw):
        self.calls.append(kw)
        system = kw.get("system", "")
        first = kw["messages"][0]["content"]
        if "contextual claims" in system:
            note = re.search(r"Note path: (\S+)\.md", first).group(1)
            out = self.gold.get(note, {"study_label": "", "claims": []})
        elif "resolve entity identity" in system:
            out = {"groups": []}
        elif "competing mechanism narratives" in system:
            out = self.synth(kw)
        elif self.agent is not None:
            return self.agent(kw, len(self.calls))
        else:
            raise AssertionError(f"unexpected call: {system[:80]}")
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(out))])


def client(**kw):
    m = Messages(**kw)
    return NS(beta=NS(messages=m)), m
