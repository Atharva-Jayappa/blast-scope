# Scoring Heuristics

Documentation for blast-scope's risk scoring logic. This file evolves as real data comes in.

## Formula (v1)

```
score = command_weight × normalized_in_degree × (1 / reversibility_factor)
```

Final score is clamped to [0.0, 1.0].

## Command Weights

| Command    | Weight | Category    |
|------------|--------|-------------|
| `rm`       | 0.9    | Destructive |
| `rmdir`    | 0.7    | Destructive |
| `truncate` | 0.8    | Destructive |
| `dd`       | 1.0    | Destructive |
| `mkfs`     | 1.0    | Destructive |
| `shred`    | 1.0    | Destructive |
| `mv`       | 0.6    | Modifying   |
| `chmod`    | 0.5    | Modifying   |
| `chown`    | 0.5    | Modifying   |
| `sed`      | 0.4    | Modifying   |
| `touch`    | 0.1    | Additive    |
| `mkdir`    | 0.1    | Additive    |
| `cp`       | 0.2    | Additive    |
| `cat`      | 0.0    | Read        |
| `ls`       | 0.0    | Read        |
| `grep`     | 0.0    | Read        |
| Unknown    | 0.3    | Default     |

Read commands always score 0.0 regardless of graph data.

## Normalized In-Degree

`normalized_in_degree = min(total_in_degree / 10, 1.0)`

- A file with 10+ direct importers from other files = maximum risk (1.0).
- A file with 0 importers but present in the graph gets a baseline of 0.1.
- A file not in the graph at all gets a baseline of 0.1 (no graph data available).

## Reversibility Factor

| Condition                        | Factor |
|----------------------------------|--------|
| Git-tracked, not recursive       | 2.0    |
| Git-tracked, recursive           | 1.0    |
| Not tracked, not recursive       | 1.0    |
| Not tracked, recursive           | 0.5    |

Higher factor = lower risk (tracked files are recoverable from git history).
Recursive operations halve the factor (more dangerous, harder to undo).

## Severity Thresholds

| Score Range | Severity | Recommendation |
|-------------|----------|----------------|
| 0.0 – 0.2  | low      | proceed        |
| 0.2 – 0.5  | medium   | confirm        |
| 0.5 – 0.8  | high     | confirm        |
| 0.8 – 1.0  | critical | block          |

## Worked Examples

### `rm -rf ./logs` (untracked, no importers)
- command_weight = 0.9
- normalized_in_degree = 0.1 (baseline, not in graph)
- reversibility_factor = 0.5 (not tracked + recursive)
- score = 0.9 × 0.1 × (1/0.5) = 0.18 → **LOW**, proceed

### `rm -rf ./config` (8 importers, not tracked)
- command_weight = 0.9
- normalized_in_degree = 0.8 (8/10)
- reversibility_factor = 0.5 (not tracked + recursive)
- score = 0.9 × 0.8 × 2.0 = 1.44 → clamped to 1.0 → **CRITICAL**, block

### `cat main.py`
- command_weight = 0.0
- score = 0.0 → **LOW**, proceed

### `mv a.py b.py` (2 importers, git-tracked)
- command_weight = 0.6
- normalized_in_degree = 0.2
- reversibility_factor = 2.0 (tracked, not recursive)
- score = 0.6 × 0.2 × 0.5 = 0.06 → **LOW**, proceed

## Known Limitations

- **Chained commands** (`&&`, `||`, `;`): only the first command is analyzed.
- **Subshell expansion** (`$(...)`, backticks): intent set to "unknown" since targets can't be statically resolved.
- **Cross-language imports**: dependency graph accuracy depends on Tree-sitter grammar support.
- **Graph staleness**: graph must be explicitly rebuilt via `index_project` — no automatic invalidation yet.
