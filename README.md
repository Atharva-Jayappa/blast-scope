# blast-scope

MCP tool that intercepts shell commands from AI agents, resolves target paths against a code dependency graph, and returns a structured risk score before execution.

Same command, completely different score based on structural consequence:

> `rm -rf ./logs` — **LOW** risk. 0 importers, not git-tracked, outside src tree. **Proceed.**

> `rm -rf ./config` — **CRITICAL**. 8 nodes import from this path, 3 are runtime-loaded. No backup detected. **Block.**

## Installation

```bash
uv add blast-scope
```

Or with pip:

```bash
pip install blast-scope
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

## Tools

### `assess_command`

Assess the blast radius of a shell command.

**Parameters:**
- `command` (required): Raw shell command string to analyze
- `cwd` (optional): Working directory for resolving relative paths
- `project_root` (optional): Project root for graph-based dependency scoring

**Returns:** Risk assessment with score (0.0-1.0), severity, rationale, affected nodes, and recommendation.

### `index_project`

Build the dependency graph for a project. Call once before using `assess_command` with `project_root`.

**Parameters:**
- `project_root` (required): Absolute path to the project root

## How It Works

1. **Parse** — `shlex`-based tokenization extracts the command, flags, targets, and intent
2. **Resolve** — Target paths are mapped to nodes in a Tree-sitter-powered dependency graph
3. **Score** — `command_weight × in_degree × (1 / reversibility_factor)` produces a 0.0-1.0 risk score

See [docs/heuristics.md](docs/heuristics.md) for the full scoring formula and worked examples.

## Development

```bash
# Install dependencies
uv sync --all-extras

# Run tests
uv run pytest -v

# Run the MCP server
uv run blast-scope
```

## Project Specification

See [CLAUDE.md](CLAUDE.md) for the full project spec, contracts, and build order.
