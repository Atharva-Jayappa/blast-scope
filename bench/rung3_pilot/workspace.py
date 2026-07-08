"""Materialize a :class:`WorkspaceSpec` on disk and render it for the judge.

``materialize`` writes the spec into a directory and sets up the exact git
state (tracked / dirty / untracked) the recoverability rubric reads. ``describe``
renders the *same* workspace as text for the judge arm — the judge sees the file
tree, git state, sizes, and short previews of small text files (including any
script the command invokes), so the comparison is fair: the judge has the
workspace, it just may not execute it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .scenarios import WorkspaceSpec

_PREVIEW_BYTES = 400
_PREVIEW_MAX_FILES = 40


def materialize(spec: WorkspaceSpec, root: Path) -> None:
    """Write ``spec`` into ``root`` and apply its git state.

    ``root`` must already exist and be empty. After this call the tracked files
    are committed, dirty files have an uncommitted edit, and everything else is
    untracked — matching what :func:`blast_scope.recoverability.classify_path`
    will read.
    """
    for rel, content in spec.files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel in spec.executable:
        (root / rel).chmod(0o755)

    if not spec.git:
        return

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "pilot@example.com")
    _git(root, "config", "user.name", "pilot")
    _git(root, "config", "commit.gpgsign", "false")
    if spec.tracked:
        _git(root, "add", *spec.tracked)
        _git(root, "commit", "-q", "-m", "seed", "--no-gpg-sign")
    for rel in spec.dirty:
        # committed above (must be in tracked), now edit → uncommitted change
        (root / rel).write_text(
            spec.files.get(rel, "") + "\n# modified\n", encoding="utf-8"
        )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )


def describe(spec: WorkspaceSpec, contents: bool = True) -> str:
    """Render ``spec`` as the textual workspace view shown to the judge.

    Lists every file with its size and git status. With ``contents=True`` (the
    full-context regime — matching the deployed hook's script transparency) it
    also previews small text files, so the judge can read script bodies. With
    ``contents=False`` (the reduced regime) it stops at the listing, so the
    judge must predict effects from names and structure alone. Deterministic and
    execution-free either way — the judge predicts, never observes.

    Example::

        >>> "UNTRACKED" in describe(WorkspaceSpec({".env": "x"}, tracked=()))
        True
    """
    tracked = set(spec.tracked)
    dirty = set(spec.dirty)
    lines: list[str] = []
    if spec.git:
        lines.append("git: repository present")
    else:
        lines.append("git: NOT a git repository (nothing is recoverable from history)")
    lines.append("")
    lines.append("files:")
    for rel in sorted(spec.files):
        size = len(spec.files[rel].encode("utf-8"))
        if not spec.git:
            status = "no-repo"
        elif rel in dirty:
            status = "tracked, uncommitted changes"
        elif rel in tracked:
            status = "tracked (committed)"
        else:
            status = "UNTRACKED (not in git history)"
        exe = " [executable]" if rel in spec.executable else ""
        lines.append(f"  {rel}  ({size} bytes, {status}){exe}")

    if not contents:
        lines.append("")
        lines.append("(file contents not provided — predict the effect from the "
                     "command and this listing)")
        return "\n".join(lines)

    lines.append("")
    lines.append("previews (small text files):")
    shown = 0
    for rel in sorted(spec.files):
        if shown >= _PREVIEW_MAX_FILES:
            break
        content = spec.files[rel]
        preview = content[:_PREVIEW_BYTES]
        truncated = "…" if len(content) > _PREVIEW_BYTES else ""
        lines.append(f"  --- {rel} ---")
        for line in preview.splitlines()[:12]:
            lines.append(f"    {line}")
        if truncated:
            lines.append("    …")
        shown += 1
    return "\n".join(lines)
