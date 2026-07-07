# Vendored from code-review-graph

- **Source:** https://github.com/tirth8205/code-review-graph
- **License:** MIT — original notice preserved verbatim in [`LICENSE`](LICENSE)
  (Copyright (c) 2026 Tirth Kanani), as MIT requires for substantial portions.
- **Commit:** 87d5265 (2026-04-11)
- **Files taken:** parser.py, graph.py, constants.py, tsconfig_resolver.py

## Modifications

- **graph.py:** Removed `networkx` dependency entirely (import, `_nxg_cache`, `_cache_lock`, `_build_networkx_graph()`, `_get_impact_radius_networkx()`). Only SQL-based graph traversal remains. Removed everything blast-scope never calls: forward impact radius (`get_impact_radius*`, `get_edges_among`), keyword/FTS search, subgraph extraction, all community/flow/signature query helpers, `node_to_dict`/`edge_to_dict`, and the schema-migration hookup (see below).
- **constants.py:** Removed `BFS_ENGINE`, `SECURITY_KEYWORDS`, `MAX_BFS_DEPTH`, `MAX_SEARCH_RESULTS` (unused after the graph.py trim).
- **migrations.py:** Deleted. All migrations (v2 signature column, v3 flows, v4 communities, v5 FTS, v6 summary tables) created state nothing here queries. Existing DBs with those tables/columns still open fine — the extra state is simply ignored.
- **parser.py, tsconfig_resolver.py:** No modifications.
