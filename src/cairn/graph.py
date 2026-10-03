"""Graph algorithms over the note network: PageRank, Louvain communities,
shortest paths and bridge detection. Pure Python, deterministic."""

from __future__ import annotations

from collections import defaultdict, deque

Graph = dict[str, dict[str, float]]  # undirected weighted adjacency


def add_edge(g: Graph, a: str, b: str, w: float) -> None:
    if a == b:
        return
    g.setdefault(a, {})
    g.setdefault(b, {})
    g[a][b] = g[a].get(b, 0.0) + w
    g[b][a] = g[b].get(a, 0.0) + w


def pagerank(nodes: list[str], edges: list[tuple[str, str]], damping: float = 0.85, iters: int = 50) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    out: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        if a != b:
            out[a].add(b)
    rank = {v: 1.0 / n for v in nodes}
    for _ in range(iters):
        dangling = sum(rank[v] for v in nodes if not out[v])
        new = {v: (1 - damping) / n + damping * dangling / n for v in nodes}
        for v in nodes:
            if out[v]:
                share = damping * rank[v] / len(out[v])
                for u in out[v]:
                    if u in new:
                        new[u] += share
        rank = new
    return rank


def louvain(g: Graph, nodes: list[str], resolution: float = 1.0) -> dict[str, int]:
    """Louvain modularity communities. Isolated nodes get their own community."""
    # node -> community at the current level; `members` maps super-nodes to original nodes
    members: dict[str, list[str]] = {v: [v] for v in nodes}
    adj: Graph = {v: dict(g.get(v, {})) for v in nodes}
    while True:
        comm, improved = _one_level(adj, resolution)
        if not improved:
            break
        # aggregate
        new_members: dict[str, list[str]] = defaultdict(list)
        for v, c in comm.items():
            new_members[f"c{c}"].extend(members[v])
        new_adj: Graph = {k: {} for k in new_members}
        for v, nbrs in adj.items():
            cv = f"c{comm[v]}"
            for u, w in nbrs.items():
                cu = f"c{comm[u]}"
                new_adj[cv][cu] = new_adj[cv].get(cu, 0.0) + w
        members, adj = dict(new_members), new_adj
    result: dict[str, int] = {}
    ordered = sorted(members.values(), key=lambda m: (-len(m), sorted(m)[0]))
    for idx, m in enumerate(ordered):
        for v in m:
            result[v] = idx
    return result


def _one_level(adj: Graph, resolution: float) -> tuple[dict[str, int], bool]:
    nodes = sorted(adj)
    idx = {v: i for i, v in enumerate(nodes)}
    comm = {v: idx[v] for v in nodes}
    # a self-loop on an aggregated node already holds 2x its internal weight
    degree = {v: sum(adj[v].values()) for v in nodes}
    m2 = sum(degree.values())
    if m2 == 0:
        return comm, False
    tot = defaultdict(float)
    for v in nodes:
        tot[comm[v]] += degree[v]
    improved_any = False
    for _ in range(20):
        moved = False
        for v in nodes:
            cv = comm[v]
            weights: dict[int, float] = defaultdict(float)
            for u, w in adj[v].items():
                if u != v:
                    weights[comm[u]] += w
            tot[cv] -= degree[v]
            best, best_gain = cv, weights.get(cv, 0.0) - resolution * tot[cv] * degree[v] / m2
            for c, w in sorted(weights.items()):
                gain = w - resolution * tot[c] * degree[v] / m2
                if gain > best_gain + 1e-12:
                    best, best_gain = c, gain
            tot[best] += degree[v]
            if best != cv:
                comm[v] = best
                moved = improved_any = True
        if not moved:
            break
    # renumber
    remap: dict[int, int] = {}
    for v in nodes:
        comm[v] = remap.setdefault(comm[v], len(remap))
    return comm, improved_any


def shortest_path(g: Graph, a: str, b: str, max_depth: int = 6) -> list[str] | None:
    if a not in g or b not in g:
        return None
    prev = {a: None}
    q = deque([(a, 0)])
    while q:
        v, d = q.popleft()
        if v == b:
            path = []
            while v is not None:
                path.append(v)
                v = prev[v]
            return path[::-1]
        if d >= max_depth:
            continue
        for u in sorted(g[v], key=lambda x: -g[v][x]):
            if u not in prev:
                prev[u] = v
                q.append((u, d + 1))
    return None


def bridge_scores(g: Graph, comm: dict[str, int]) -> dict[str, float]:
    """Participation coefficient: how evenly a node's links spread across communities.
    High values mark notes that connect otherwise separate topics."""
    scores = {}
    for v, nbrs in g.items():
        total = sum(nbrs.values())
        if total == 0 or len(nbrs) < 2:
            scores[v] = 0.0
            continue
        per: dict[int, float] = defaultdict(float)
        for u, w in nbrs.items():
            per[comm.get(u, -1)] += w
        scores[v] = 1.0 - sum((w / total) ** 2 for w in per.values())
    return scores
