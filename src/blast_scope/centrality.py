"""Weighted PageRank over the dependency graph — pure Python, no numpy/scipy.

Raw in-degree (how many edges point at a file) is a shallow proxy for blast
radius: a file imported by two leaf scripts looks identical to one imported by
two hub modules that the whole app routes through. PageRank fixes this — a node
is important if *important* nodes depend on it, computed transitively over the
whole graph.

Edges run ``source → target`` meaning "source depends on target" (``a.py``
``IMPORTS_FROM`` ``b.py`` is an edge ``a → b``). So importance accumulates on
the depended-upon ``target`` side, which is exactly the blast-radius signal we
want: deleting a high-PageRank file breaks a lot.

Edge kinds are weighted — a runtime ``IMPORTS_FROM`` carries more structural
consequence than a ``TESTED_BY`` or ``CONTAINS`` edge.
"""

from __future__ import annotations

# Structural significance of each edge kind. Higher = a heavier dependency.
EDGE_WEIGHTS: dict[str, float] = {
    "IMPORTS_FROM": 1.0,
    "DEPENDS_ON": 1.0,
    "INHERITS": 0.9,
    "IMPLEMENTS": 0.9,
    "CALLS": 0.8,
    "REFERENCES": 0.5,
    "CONTAINS": 0.3,
    "TESTED_BY": 0.2,
}
DEFAULT_EDGE_WEIGHT: float = 0.5


def edge_weight(kind: str) -> float:
    """Return the structural weight for an edge kind.

    Example::

        >>> edge_weight("IMPORTS_FROM")
        1.0
        >>> edge_weight("MYSTERY_EDGE")
        0.5
    """
    return EDGE_WEIGHTS.get(kind, DEFAULT_EDGE_WEIGHT)


def pagerank(
    edges: list[tuple[str, str, str]],
    *,
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1.0e-6,
) -> dict[str, float]:
    """Compute weighted PageRank over a dependency graph.

    Args:
        edges: ``(source, target, kind)`` triples. ``source`` depends on
            ``target``; importance flows toward ``target``.
        damping: Probability of following an edge vs. teleporting (classic 0.85).
        max_iter: Hard cap on power-iteration steps.
        tol: L1-convergence threshold for early stopping.

    Returns:
        ``{qualified_name: score}`` with scores normalized so the most central
        node is ``1.0``. Returns ``{}`` for an empty graph.

    Example::

        >>> pr = pagerank([("a", "c", "IMPORTS_FROM"), ("b", "c", "IMPORTS_FROM")])
        >>> max(pr, key=pr.get)
        'c'
    """
    if not edges:
        return {}

    # Build node set and weighted outgoing adjacency.
    nodes: set[str] = set()
    out: dict[str, list[tuple[str, float]]] = {}
    out_weight: dict[str, float] = {}
    for source, target, kind in edges:
        nodes.add(source)
        nodes.add(target)
        w = edge_weight(kind)
        out.setdefault(source, []).append((target, w))
        out_weight[source] = out_weight.get(source, 0.0) + w

    n = len(nodes)
    if n == 0:
        return {}

    base = (1.0 - damping) / n
    rank: dict[str, float] = {node: 1.0 / n for node in nodes}

    for _ in range(max_iter):
        # Dangling nodes (no outgoing edges) redistribute their mass uniformly.
        dangling = sum(rank[node] for node in nodes if out_weight.get(node, 0.0) == 0.0)
        dangling_share = damping * dangling / n

        new_rank: dict[str, float] = {node: base + dangling_share for node in nodes}
        for source, targets in out.items():
            src_rank = rank[source]
            total_w = out_weight[source]
            if total_w == 0.0:
                continue
            contribution = damping * src_rank / total_w
            for target, w in targets:
                new_rank[target] += contribution * w

        delta = sum(abs(new_rank[node] - rank[node]) for node in nodes)
        rank = new_rank
        if delta < tol:
            break

    peak = max(rank.values())
    if peak <= 0.0:
        return {node: 0.0 for node in nodes}
    return {node: score / peak for node, score in rank.items()}
