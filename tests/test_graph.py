from cairn.graph import add_edge, bridge_scores, louvain, pagerank, shortest_path
from cairn.similarity import TfIdf, tokenize


def two_cliques():
    g = {}
    for grp in (["a", "b", "c", "d"], ["w", "x", "y", "z"]):
        for i, u in enumerate(grp):
            for v in grp[i + 1:]:
                add_edge(g, u, v, 1.0)
    add_edge(g, "d", "w", 0.2)
    return g


def test_louvain_separates_cliques():
    g = two_cliques()
    comm = louvain(g, sorted(g) + ["lonely"])
    assert len({comm[v] for v in "abcd"}) == 1
    assert len({comm[v] for v in "wxyz"}) == 1
    assert comm["a"] != comm["w"]
    assert comm["lonely"] not in (comm["a"], comm["w"])


def test_shortest_path_and_bridges():
    g = two_cliques()
    assert shortest_path(g, "a", "z")[0] == "a" and shortest_path(g, "a", "z")[-1] == "z"
    comm = louvain(g, sorted(g))
    b = bridge_scores(g, comm)
    assert b["d"] > b["a"]


def test_pagerank_prefers_linked():
    r = pagerank(["a", "b", "c"], [("a", "c"), ("b", "c")])
    assert r["c"] > r["a"]


def test_tfidf_neighbours():
    t = TfIdf({"1": "sleep memory consolidation", "2": "memory consolidation during sleep", "3": "tax returns"})
    nb = t.neighbors(2, 0.1)
    assert nb["1"][0][0] == "2" and nb["3"] == []
    assert "the" not in tokenize("the sleeping notes")
