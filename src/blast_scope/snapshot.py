"""Snapshot and undo for risky commands.

Before a high-risk command runs, blast-scope tars the paths it is about to
touch into ``<root>/.blast-scope/snapshots/<id>/`` so the change can be undone.

Why a tarball and not just git: the most dangerous targets are exactly the ones
git can't recover — untracked files, ``.env``, ``*.tfstate``, a ``.gitignore``-d
``dist/``. A tar of the literal bytes is the only universal undo. Snapshots
live under ``.blast-scope/`` which is already git-ignored.
"""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import TypedDict

from blast_scope.recoverability import classify_path

logger = logging.getLogger(__name__)

_SNAPSHOT_SUBDIR = Path(".blast-scope") / "snapshots"
_ARCHIVE_NAME = "data.tar.gz"
_MANIFEST_NAME = "manifest.json"

# Recoverability categories a snapshot would only duplicate: git history or a
# cheap rebuild already covers them. Driving the skip off ``classify_path``
# keeps recoverability.py the single source of truth — we never re-derive
# "is this safe to lose" with a second set of patterns here.
_RECOVERABLE_CATEGORIES: frozenset[str] = frozenset(
    {"tracked_clean", "regenerable", "absent"}
)

# Hard ceiling on what one snapshot will archive. Taring a multi-GB tree to
# "protect" it stalls the agent and can fill the disk — strictly worse than the
# risk of losing something that large and regenerable-by-nature. Oversize
# targets are reported to the caller (which warns), never silently tarred.
_MAX_SNAPSHOT_BYTES: int = 256 * 1024 * 1024  # 256 MiB


class SnapshotEntry(TypedDict):
    """One archived path within a snapshot."""

    member: str  # name inside the tarball
    original: str  # absolute path it was taken from / restores to
    is_dir: bool


class Snapshot(TypedDict):
    """Manifest describing a single snapshot.

    Example::

        {"id": "20260530T101500-a1b2c3", "created": 1.7e9, "reason": "rm -rf ./config",
         "root": "/proj", "entries": [{"member": "0", "original": "/proj/config", ...}]}
    """

    id: str
    created: float
    reason: str
    root: str
    entries: list[SnapshotEntry]


class SnapshotPlan(TypedDict):
    """The decision of *what* to snapshot, before any tarball is written.

    Separates policy (which targets are worth and safe to archive) from the
    mechanism in :func:`create_snapshot` (tar the given paths). Driven entirely
    by :func:`blast_scope.recoverability.classify_path` and a size cap.

    Example::

        {"archive": ["/proj/.env"], "skipped_recoverable": ["/proj/node_modules"],
         "skipped_oversize": ["/proj/data"]}
    """

    archive: list[str]              # existing, non-recoverable, within-cap → tar these
    skipped_recoverable: list[str]  # git-clean / regenerable → no snapshot needed
    skipped_oversize: list[str]     # over the size cap → warn, don't tar


def snapshots_dir(root: Path | str) -> Path:
    """Return the snapshot storage directory for a project ``root``."""
    return Path(root) / _SNAPSHOT_SUBDIR


def plan_snapshot(
    targets: list[str | Path], *, max_bytes: int = _MAX_SNAPSHOT_BYTES
) -> SnapshotPlan:
    """Decide which targets to archive, using recoverability + a size cap.

    A target is *not* archived when either:

    - its recoverability category is one git or a rebuild already covers
      (``tracked_clean``, ``regenerable``, ``absent``) — snapshotting it would
      be redundant; or
    - it is larger than ``max_bytes`` — taring it would stall the agent / fill
      the disk, so it is reported as oversize for the caller to warn about.

    Everything else is returned in ``archive`` for :func:`create_snapshot`.

    Args:
        targets: Absolute paths a destructive command is about to hit.
        max_bytes: Hard cap on a single target's size; defaults to 256 MiB.

    Returns:
        A :class:`SnapshotPlan` partitioning the targets.

    Example::

        >>> plan_snapshot(["/proj/.env", "/proj/node_modules"])["archive"]
        ['/proj/.env']
    """
    archive: list[str] = []
    skipped_recoverable: list[str] = []
    skipped_oversize: list[str] = []

    for t in targets:
        path = Path(t)
        # Nothing on disk → nothing to lose (create_snapshot would skip it too).
        if not (path.exists() or path.is_symlink()):
            continue
        if classify_path(path)["category"] in _RECOVERABLE_CATEGORIES:
            skipped_recoverable.append(str(path))
            continue
        if _exceeds_cap(path, max_bytes):
            skipped_oversize.append(str(path))
            continue
        archive.append(str(path))

    return SnapshotPlan(
        archive=archive,
        skipped_recoverable=skipped_recoverable,
        skipped_oversize=skipped_oversize,
    )


def create_snapshot(
    targets: list[str | Path], *, root: Path | str, reason: str = ""
) -> Snapshot | None:
    """Archive every existing target so the command can be undone.

    Non-existent targets are skipped (deleting them has nothing to back up).
    Returns ``None`` if nothing existed to snapshot.

    Args:
        targets: Absolute paths the command is about to modify or delete.
        root: Project root; the snapshot is stored under ``root/.blast-scope``.
        reason: Free-text note (typically the command) stored in the manifest.

    Returns:
        The created :class:`Snapshot` manifest, or ``None`` if no target existed.

    Example::

        >>> create_snapshot(["/proj/config"], root="/proj", reason="rm -rf ./config")
        {"id": "...", "entries": [...], ...}
    """
    existing = [Path(t) for t in targets if Path(t).exists() or Path(t).is_symlink()]
    if not existing:
        return None

    sid = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    sdir = snapshots_dir(root) / sid
    sdir.mkdir(parents=True, exist_ok=True)

    entries: list[SnapshotEntry] = []
    with tarfile.open(sdir / _ARCHIVE_NAME, "w:gz") as tar:
        for i, path in enumerate(existing):
            member = str(i)
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            tar.add(path, arcname=member)
            entries.append(
                SnapshotEntry(member=member, original=str(resolved), is_dir=path.is_dir())
            )

    manifest = Snapshot(
        id=sid,
        created=time.time(),
        reason=reason,
        root=str(Path(root).resolve()),
        entries=entries,
    )
    (sdir / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("snapshot %s captured %d path(s)", sid, len(entries))
    return manifest


def list_snapshots(root: Path | str) -> list[Snapshot]:
    """Return all snapshots for ``root``, newest first.

    Example::

        >>> list_snapshots("/proj")
        [{"id": "20260530T101500-a1b2c3", ...}]
    """
    base = snapshots_dir(root)
    if not base.is_dir():
        return []
    out: list[Snapshot] = []
    for sub in base.iterdir():
        manifest = sub / _MANIFEST_NAME
        if manifest.is_file():
            try:
                out.append(json.loads(manifest.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                logger.debug("skipping unreadable manifest %s", manifest)
    out.sort(key=lambda s: s["id"], reverse=True)
    return out


def restore_snapshot(snapshot_id: str, *, root: Path | str) -> list[str]:
    """Restore a snapshot, overwriting whatever is at the original paths.

    Each archived path is extracted to a temp dir and moved back onto its
    original location (replacing any current file/dir there).

    Args:
        snapshot_id: The id returned by :func:`create_snapshot`.
        root: Project root the snapshot was taken under.

    Returns:
        The list of original paths that were restored.

    Raises:
        FileNotFoundError: If the snapshot or its archive is missing.

    Example::

        >>> restore_snapshot("20260530T101500-a1b2c3", root="/proj")
        ["/proj/config"]
    """
    sdir = snapshots_dir(root) / snapshot_id
    manifest_path = sdir / _MANIFEST_NAME
    archive_path = sdir / _ARCHIVE_NAME
    if not manifest_path.is_file() or not archive_path.is_file():
        raise FileNotFoundError(f"no snapshot {snapshot_id!r} under {snapshots_dir(root)}")

    manifest: Snapshot = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive_path, "r:gz") as tar:
            try:
                tar.extractall(tmp, filter="data")  # py3.12+/backports: safe extraction
            except TypeError:
                tar.extractall(tmp)  # older 3.11.x without the filter kwarg

        for entry in manifest["entries"]:
            original = Path(entry["original"])
            staged = Path(tmp) / entry["member"]
            original.parent.mkdir(parents=True, exist_ok=True)
            _remove(original)
            shutil.move(str(staged), str(original))
            restored.append(str(original))

    logger.info("snapshot %s restored %d path(s)", snapshot_id, len(restored))
    return restored


def _exceeds_cap(path: Path, cap: int) -> bool:
    """True if ``path`` is larger than ``cap`` bytes (short-circuits early).

    Directories are summed file-by-file and the walk stops the moment the cap
    is crossed, so an oversized tree is never fully traversed. Symlinks are not
    followed (the archive stores the link itself, not its target).

    Example::

        >>> _exceeds_cap(Path("/proj/node_modules"), 256 * 1024 * 1024)
        True
    """
    if path.is_dir() and not path.is_symlink():
        total = 0
        for child in path.rglob("*"):
            if child.is_symlink() or not child.is_file():
                continue
            try:
                total += child.stat().st_size
            except OSError:
                continue
            if total > cap:
                return True
        return False
    try:
        return path.lstat().st_size > cap
    except OSError:
        return False


def _remove(path: Path) -> None:
    """Delete whatever currently lives at ``path`` (file, dir, or symlink)."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
