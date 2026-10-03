"""Lexical similarity between notes (TF-IDF + cosine), no external deps."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having
he her here hers herself him himself his how i if in into is it its itself just like may me might more most
much must my myself no nor not now of off on once only or other our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these they this those through to
too under until up us very was we were what when where which while who whom why will with would you your
yours yourself yourselves one two get got make made use used using way well also e.g i.e etc via per
note notes todo http https www com md
""".split())

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']{1,}")


def _stem(w: str) -> str:
    # deliberately light: plural/verb suffixes only, keeps terms readable as labels
    for suf in ("ies", "sses", "ing", "edly", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "ies":
                return w[:-3] + "y"
            if suf == "sses":
                return w[:-2]
            if suf == "es" and not w.endswith(("ches", "shes", "xes", "zes")):
                return w[:-1]
            return w[: -len(suf)]
    return w


def tokenize(text: str) -> list[str]:
    out = []
    for t in TOKEN_RE.findall(text.lower()):
        t = t.strip("-'")
        if len(t) < 3 or t in STOPWORDS:
            continue
        out.append(_stem(t))
    return out


class TfIdf:
    def __init__(self, docs: dict[str, str]):
        self.ids = list(docs)
        counts = {i: Counter(tokenize(t)) for i, t in docs.items()}
        df: Counter = Counter()
        for c in counts.values():
            df.update(c.keys())
        n = max(len(docs), 1)
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items()}
        self.vectors: dict[str, dict[str, float]] = {}
        for i, c in counts.items():
            vec = {t: (1 + math.log(f)) * self.idf[t] for t, f in c.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self.vectors[i] = {t: v / norm for t, v in vec.items()}
        self.postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for i, vec in self.vectors.items():
            for t, w in vec.items():
                self.postings[t].append((i, w))

    def top_terms(self, ids: list[str], k: int = 5) -> list[str]:
        agg: Counter = Counter()
        for i in ids:
            for t, w in self.vectors.get(i, {}).items():
                agg[t] += w
        return [t for t, _ in agg.most_common(k)]

    def neighbors(self, k: int, min_score: float) -> dict[str, list[tuple[str, float]]]:
        """Top-k cosine neighbours for every document, via the inverted index."""
        # very common terms add cost and little signal: skip terms in >50% of docs
        cutoff = max(2, int(len(self.ids) * 0.5))
        result: dict[str, list[tuple[str, float]]] = {}
        for i, vec in self.vectors.items():
            scores: dict[str, float] = defaultdict(float)
            for t, w in vec.items():
                plist = self.postings[t]
                if len(plist) > cutoff:
                    continue
                for j, wj in plist:
                    if j != i:
                        scores[j] += w * wj
            best = sorted(((j, s) for j, s in scores.items() if s >= min_score), key=lambda x: (-x[1], x[0]))
            result[i] = best[:k]
        return result
