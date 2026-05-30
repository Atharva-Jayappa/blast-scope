"""Tests for blast_scope.centrality (weighted PageRank)."""

from __future__ import annotations

from blast_scope.centrality import EDGE_WEIGHTS, edge_weight, pagerank


class TestEdgeWeight:
    def test_known_kind(self) -> None:
        assert edge_weight("IMPORTS_FROM") == 1.0
        assert edge_weight("TESTED_BY") == 0.2

    def test_unknown_kind_falls_back(self) -> None:
        assert edge_weight("SOMETHING_NEW") == 0.5


class TestPageRank:
    def test_empty_graph(self) -> None:
        assert pagerank([]) == {}

    def test_normalized_to_one(self) -> None:
        pr = pagerank([("a", "b", "IMPORTS_FROM")])
        assert max(pr.values()) == 1.0

    def test_depended_upon_node_ranks_highest(self) -> None:
        # Both a and b import c → c is the most central.
        pr = pagerank([
            ("a", "c", "IMPORTS_FROM"),
            ("b", "c", "IMPORTS_FROM"),
        ])
        assert max(pr, key=pr.get) == "c"
        assert pr["c"] > pr["a"]
        assert pr["c"] > pr["b"]

    def test_transitive_importance(self) -> None:
        # a → b → c : importance accumulates downstream toward c.
        pr = pagerank([
            ("a", "b", "IMPORTS_FROM"),
            ("b", "c", "IMPORTS_FROM"),
        ])
        assert pr["c"] >= pr["b"] >= pr["a"]

    def test_edge_weight_affects_rank(self) -> None:
        # Same source points at two sinks, but via different edge kinds.
        # The heavier IMPORTS_FROM edge should give its target more rank.
        pr = pagerank([
            ("a", "strong", "IMPORTS_FROM"),
            ("a", "weak", "TESTED_BY"),
        ])
        assert pr["strong"] > pr["weak"]

    def test_converges_on_cycle(self) -> None:
        # A cycle must not blow up or hang; it should converge and normalize.
        pr = pagerank([
            ("a", "b", "CALLS"),
            ("b", "c", "CALLS"),
            ("c", "a", "CALLS"),
        ])
        assert max(pr.values()) == 1.0
        assert all(0.0 <= v <= 1.0 for v in pr.values())
