# Privacy Policy — blast-scope

**Effective: 2026-07-05**

blast-scope runs entirely on your machine. It has no server side, no accounts,
and no telemetry.

## What blast-scope processes

To score a command, blast-scope reads — **locally, on your machine only**:

- the shell command text and working directory passed to it by your agent
  (via the MCP tool call or the PreToolUse hook payload);
- filesystem metadata for the command's targets (existence, size, symlinks,
  glob matches);
- your project's git state (tracked/modified/untracked status, refs), via
  local read-only `git` commands;
- project files needed for analysis: source files (to build the local
  dependency graph), `package.json`, Makefiles, and scripts referenced by the
  command (script transparency), and lockfiles;
- local Docker/SQLite state via strictly read-only probes, when the command
  being scored involves them.

## What blast-scope stores

- A dependency-graph database at `<project>/.blast-scope/graph.db`.
- Undo snapshots (tarballs of files a critical command would destroy) at
  `<project>/.blast-scope/snapshots/`.

Both live inside your project directory, never leave your machine, and can be
deleted at any time.

## What blast-scope transmits

**Nothing.** blast-scope makes no network requests. It does not collect,
transmit, or share any data with the author or any third party. There is no
telemetry, no analytics, no crash reporting, and no phoning home.

## Third parties

None. Installation is served by the package registry you install from (PyPI /
GitHub), which sees only the download; blast-scope itself contacts no external
service at runtime.

## Changes

Any change to this policy will appear in this file's git history in the
[blast-scope repository](https://github.com/Atharva-Jayappa/blast-scope).

## Contact

Questions: open an issue at
https://github.com/Atharva-Jayappa/blast-scope/issues or email
jatharva289@gmail.com.
