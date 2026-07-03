# Vendored from code-review-graph

- **Source:** https://github.com/tirth8205/code-review-graph
- **License:** MIT — original notice preserved verbatim in [`LICENSE`](LICENSE)
  (Copyright (c) 2026 Tirth Kanani), as MIT requires for substantial portions.
- **Commit:** 87d5265 (2026-04-11)
- **Files taken:** parser.py, graph.py, constants.py, migrations.py, tsconfig_resolver.py

## Modifications

- **graph.py:** Removed `networkx` dependency entirely (import, `_nxg_cache`, `_cache_lock`, `_build_networkx_graph()`, `_get_impact_radius_networkx()`). Only SQL-based graph traversal remains.
- **constants.py:** Removed `BFS_ENGINE` constant (always SQL now).
- **parser.py, tsconfig_resolver.py, migrations.py:** No modifications.
