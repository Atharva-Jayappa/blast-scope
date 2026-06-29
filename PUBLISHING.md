# Publishing blast-scope

The repo is ship-ready: packaging metadata, an MIT `LICENSE`, an MCP registry
manifest (`server.json`), and a Claude Code plugin (`.claude-plugin/`,
`.mcp.json`, `hooks/`) are all in place. The steps below need *your* accounts and
are the only things not already done — run them in order.

## 1. PyPI (the gate — everything downstream needs this)

```bash
uv build                      # produces dist/*.whl and dist/*.tar.gz (already verified)
uvx twine upload dist/*       # needs a PyPI account + API token
```

- Verify the install works clean: `uvx blast-scope` (from a fresh shell).
- The README already carries the registry ownership token
  (`<!-- mcp-name: io.github.atharva-jayappa/blast-scope -->`), so the PyPI
  description satisfies the next step automatically.

## 2. Official MCP registry (most directories crawl from here)

`registry.modelcontextprotocol.io` has no human review — it verifies ownership
and schema only.

```bash
brew install mcp-publisher          # or grab the prebuilt binary
mcp-publisher login github          # ties the io.github.atharva-jayappa namespace to your GH account
mcp-publisher publish               # reads server.json
```

`server.json` is pre-filled; `mcp-publisher init` is authoritative if the schema
has moved on — diff its output against the committed file and reconcile the
version/identifier fields.

Then **claim** your auto-ingested listings on PulseMCP, Glama, and mcp.so, and
open a PR to [`punkpeye/awesome-mcp-servers`](https://github.com/punkpeye/awesome-mcp-servers)
(there's no clear "security/guardrails" leader yet — own that slot).

## 3. Claude Code plugin

Already works straight from the repo — no gatekeeper:

```text
/plugin marketplace add Atharva-Jayappa/blast-scope
/plugin install blast-scope
```

That installs the MCP server **and** the advisory PreToolUse hook in one step.
The hook runs `uvx --from blast-scope blast-scope-hook`; if you'd rather avoid
per-command `uvx` startup, `uv tool install blast-scope` puts `blast-scope-hook`
on PATH and you can point the hook at it directly.

Once polished, submit to Anthropic's curated directory via the
[plugin directory submission form](https://clau.de/plugin-directory-submission).

## 4. Announce

The killer demo is already the README headline: **same command, opposite score —
`rm -rf ./logs` LOW vs `rm -rf ./config` CRITICAL.** Lead a Show HN with the
contrast and the SABER numbers (0.4% FPR, 82% on data-destruction), Tue–Thu
morning ET, and work the thread for the first few hours. Cross-post to
r/ClaudeAI and r/commandline framed as "I built this because my agent `rm -rf`'d
my config."

---

**Note on the license:** MIT is set as a sensible default (and the project
vendors MIT-licensed code from code-review-graph). Change `LICENSE` and the
`license` field in `pyproject.toml` if you want something else before publishing.
