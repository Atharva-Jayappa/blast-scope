# Blast Scope

MCP tool that scores the blast radius of shell commands before execution. It parses commands, resolves target paths against a Tree-sitter-powered dependency graph, and returns a structured risk assessment — not a blocklist, but a contextual risk score.

Same command, completely different score based on structural consequence:

> `rm -rf ./logs` — **LOW** risk. No graph dependencies, no importers. **Proceed.**

> `rm -rf ./config` — **CRITICAL**. Multiple nodes import from this path, high in-degree. **Block.**

## Status

**v0.1.0 — early development.** Core pipeline works end-to-end: parse → resolve → score. Not yet published to PyPI.

What's implemented:
- `shlex`-based command parser with intent classification (destructive / additive / read / unknown)
- `sudo` stripping, redirect extraction, subshell detection, recursive flag parsing
- Git-based reversibility checks (`git ls-files`)
- Tree-sitter dependency graph (vendored from [code-review-graph](https://github.com/tirth8205/code-review-graph))
- In-degree-based risk scoring with configurable command weights
- MCP server (stdio) exposing two tools: `assess_command` and `index_project`

What's not yet implemented:
- Runtime-load detection
- Backup/snapshot detection
- Chained command splitting (`&&`, `;`, `|` treated as a single command)
- Auto-indexing (you must call `index_project` before graph-aware scoring)

## Installation

Not yet on PyPI. Install from source:

```bash
git clone https://github.com/Atharva-Jayappa/blast-scope.git
cd blast-scope
uv sync --all-extras
```

Or install directly from GitHub:

```bash
uv pip install git+https://github.com/Atharva-Jayappa/blast-scope.git
```

## MCP Configuration

Add to your MCP client config (e.g. Claude Code `settings.json`):

```json
{
  "mcpServers": {
    "blast-scope": {
      "command": "blast-scope",
      "type": "stdio"
    }
  }
}
```

If installed from source with `uv`, use the full path or run via `uv run blast-scope`.

## Tools

### `assess_command`

Assess the blast radius of a shell command.

**Parameters:**
- `command` (required): Raw shell command string to analyze
- `cwd` (optional): Working directory for resolving relative paths
- `project_root` (optional): Project root for graph-based dependency scoring

**Returns:** Risk assessment with score (0.0–1.0), severity, rationale, affected nodes, and recommendation.

### `index_project`

Build the dependency graph for a project. Call this before using `assess_command` with `project_root` — the graph is stored in `.blast-scope/graph.db` under the project root.

**Parameters:**
- `project_root` (required): Absolute path to the project root

## How It Works

1. **Parse** — `shlex`-based tokenization extracts the command, flags, target paths, and intent (destructive / additive / read / unknown)
2. **Resolve** — Target paths are mapped to nodes in a Tree-sitter-powered dependency graph stored in SQLite. In-degree (how many other files reference a target) is the key signal.
3. **Score** — `command_weight × normalized_in_degree × (1 / reversibility_factor)` produces a 0.0–1.0 risk score, mapped to severity (low / medium / high / critical) and a recommendation (proceed / confirm / block).

See [docs/heuristics.md](docs/heuristics.md) for the full scoring formula, weight tables, and worked examples.

## Development

```bash
uv sync --all-extras

uv run pytest -v

uv run blast-scope
```

## Project Structure

```
blast-scope/
├── src/blast_scope/
│   ├── server.py            # MCP server, two tools
│   ├── command_parser.py    # shell command → structured intent
│   ├── graph_resolver.py    # paths → dependency graph nodes
│   ├── risk_scorer.py       # signals → risk score + rationale
│   └── vendor/crg/          # vendored from code-review-graph (MIT)
├── tests/
│   ├── fixtures/            # test command strings + sample project
│   ├── test_command_parser.py
│   ├── test_graph_resolver.py
│   ├── test_risk_scorer.py
│   └── test_e2e.py
└── docs/
    └── heuristics.md
```

## License

See [CLAUDE.md](CLAUDE.md) for the full project spec, contracts, and build order.
