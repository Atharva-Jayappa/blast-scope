"""Speculative execution — the ground-truth oracle (rung 2, tier 2).

Every other analyzer *predicts* what a command destroys. This one *observes*:
it runs the command against a disposable copy-on-write copy of the working
tree, then diffs the scratch layer to learn exactly which files were created,
modified, or deleted. No rewriting, no heuristics — the kernel does the work.

**This is the one module that executes the analyzed command.** That makes its
single most important property non-negotiable: a bug here must never let the
command touch the real filesystem. The isolation rests on independent layers,
any one of which suffices:

1. **overlayfs lowerdir is never written.** The real working tree is the
   overlay's read-only lower layer; the kernel writes all changes to a scratch
   upper layer on a throwaway tmpdir. This is a filesystem guarantee, not our
   code.
2. **A private mount namespace.** The overlay is mounted inside an
   ``unshare --mount`` namespace, so it cannot leak into the parent mount
   table even if teardown is skipped.
3. **A severed network namespace.** ``unshare --net`` gives the command no
   network interfaces, so speculation cannot exfiltrate or phone home while
   "just previewing".
4. **A speculability gate.** :func:`is_speculable` refuses commands whose
   effects escape a filesystem overlay — network fetches, absolute writes
   outside cwd, ``sudo``, device access, or non-idempotent externalities
   (``git push``, ``docker``, sending mail).
5. **Opt-in only.** Nothing here runs unless ``BLAST_SCOPE_SPECULATE`` is set.
   It is never on the default hook path.

Availability is narrow by construction: Linux with unprivileged user +
overlay namespaces (kernel ≥ 5.11, not disabled by the distro). Everywhere
else :func:`speculate` returns an unavailable result and the caller falls back
to the static oracles. That is the intended design, not a gap.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 4.0
_MAX_DIFF_ENTRIES = 500
# Bound the walk so a command that creates a huge tree in the overlay can't
# turn a preview into a multi-minute filesystem crawl.
_MAX_WALK_ENTRIES = 20000


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Speculation:
    """The observed effect of running a command in the overlay sandbox.

    Paths are relative to the working directory. ``ran`` is True only when the
    command actually executed in a sandbox; otherwise ``reason`` explains why
    (not speculable, unavailable, timed out) and the path sets are empty.

    Example::

        Speculation(ran=True, deleted=("config.py",), reason="observed")
    """

    ran: bool
    reason: str
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    exit_code: int | None = None
    truncated: bool = False

    @property
    def touched(self) -> tuple[str, ...]:
        """All paths the command created, modified, or deleted."""
        return self.created + self.modified + self.deleted

    @property
    def destroyed(self) -> tuple[str, ...]:
        """Paths the command deleted or overwrote — the recoverability concern."""
        return self.deleted + self.modified


# ---------------------------------------------------------------------------
# Speculability — is it SAFE to run this command in the sandbox at all?
# ---------------------------------------------------------------------------

# Verbs whose effects a filesystem overlay cannot contain or that produce
# non-idempotent externalities. Running these "to preview" is itself the harm.
_NON_SPECULABLE_VERBS: frozenset[str] = frozenset(
    {
        # network fetch / exfil
        "curl", "wget", "scp", "sftp", "ssh", "rsync", "nc", "ncat", "telnet",
        "ftp", "aws", "gcloud", "az", "gsutil",
        # publish / push external state
        "git",  # push/fetch/remote — and we already have a rich git oracle
        "docker", "podman", "kubectl", "helm", "terraform", "systemctl",
        "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go", "gem",
        "apt", "apt-get", "yum", "dnf", "brew", "snap",
        # mail / messaging
        "mail", "mailx", "sendmail", "mutt",
        # privilege / device / kernel
        "sudo", "doas", "su", "mount", "umount", "mknod", "dd", "mkfs",
        "fdisk", "parted", "chown", "chroot", "insmod", "modprobe",
        "systemd-run", "kill", "killall", "pkill", "reboot", "shutdown",
        # process substitution into a shell we can't see
        "eval", "exec", "source",
    }
)


@dataclass(frozen=True)
class Speculability:
    """Whether a command may be run in the sandbox, and why not if not."""

    ok: bool
    reason: str
    rejected_by: str = ""  # the token/segment that disqualified it


def is_speculable(
    resolved_command: str,
    parsed_segments: Sequence[dict],
    cwd: Path,
) -> Speculability:
    """Decide whether running ``resolved_command`` in the sandbox is safe.

    Deny-by-default on anything a filesystem overlay can't contain: network
    verbs, ``sudo``/device access, external-state mutations (already covered
    by dedicated oracles), and absolute paths that point outside ``cwd`` (the
    overlay only shadows the working tree — an absolute write elsewhere would
    hit the real filesystem).

    Args:
        resolved_command: The fully resolved command chain (post-resolution).
        parsed_segments: The per-segment ``ParsedCommand`` dicts.
        cwd: The working directory the overlay will shadow.

    Returns:
        A :class:`Speculability`. ``ok`` is True only when every segment is
        containable.

    Example::

        >>> is_speculable("rm -rf build", [{"command": "rm", "targets": []}], Path.cwd()).ok
        True
    """
    try:
        cwd_res = cwd.resolve()
    except OSError:
        return Speculability(False, "working directory does not resolve")

    for seg in parsed_segments:
        verb = seg.get("command", "")
        if verb in _NON_SPECULABLE_VERBS:
            return Speculability(
                False,
                f"'{verb}' has effects an overlay can't contain (network / external "
                f"state / privilege) — analyzed statically, not speculatively",
                rejected_by=verb,
            )
        # Any absolute target outside the overlayed tree would escape to the
        # real filesystem. Relative targets and absolutes under cwd are fine.
        for t in list(seg.get("targets", ())) + list(seg.get("write_targets", ()) or ()):
            if _escapes_tree(t, cwd_res):
                return Speculability(
                    False,
                    f"target '{t}' is outside the sandboxed working tree — an "
                    f"absolute write there would hit the real filesystem",
                    rejected_by=t,
                )

    # Command substitution / opaque wrappers survived resolution → we don't
    # actually know what would run; never execute a black box.
    if "$(" in resolved_command or "`" in resolved_command:
        return Speculability(
            False, "unresolved command substitution — refusing to execute a black box"
        )
    return Speculability(True, "containable in a filesystem overlay")


def _escapes_tree(target: str, cwd_res: Path) -> bool:
    """True if an absolute target resolves outside the sandboxed tree.

    POSIX-absolute (leading ``/``) is honored even when the host is Windows —
    the analyzed commands are POSIX, and the gate is deny-by-default.
    """
    p = Path(target)
    if not p.is_absolute() and not target.startswith("/"):
        return False  # relative → under cwd, contained
    try:
        p_res = p.resolve()
    except OSError:
        return True  # can't tell → treat as escape (safe default)
    try:
        p_res.relative_to(cwd_res)
        return False
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Capability detection (cached)
# ---------------------------------------------------------------------------

_capability_cache: bool | None = None


def available() -> bool:
    """True if this host can run the overlay sandbox (Linux + unpriv namespaces).

    Result is cached for the process. Checks are cheap and read-only: OS is
    Linux, ``unshare`` exists, and a trivial user+mount+net namespace with an
    overlay mount actually succeeds (some distros disable unprivileged
    overlay even on new kernels — the only reliable test is to try it).
    """
    global _capability_cache
    if _capability_cache is not None:
        return _capability_cache
    _capability_cache = _probe_capability()
    return _capability_cache


def _probe_capability() -> bool:
    if platform.system() != "Linux":
        return False
    if shutil.which("unshare") is None:
        return False
    # Actually attempt the exact operation we rely on, in a throwaway dir.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for d in ("lower", "upper", "work", "merged"):
                (base / d).mkdir()
            script = _overlay_script(base, "true")
            proc = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--mount", "--net",
                 "bash", "-c", script],
                capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT,
            )
            return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def reset_capability_cache() -> None:
    """Clear the cached capability probe (tests)."""
    global _capability_cache
    _capability_cache = None


# ---------------------------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------------------------


def speculate(
    command: str,
    cwd: Path,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Speculation:
    """Run ``command`` against a CoW overlay of ``cwd``; return the observed diff.

    The real ``cwd`` is the overlay's read-only lower layer and is never
    written. Callers MUST have gated on :func:`is_speculable` first; this
    function assumes the command is containable and only handles the mechanics
    and failure modes.

    Returns a :class:`Speculation` with ``ran=False`` and a reason when the
    sandbox is unavailable or the run fails — never raises.
    """
    if not available():
        return Speculation(False, "overlay sandbox unavailable on this host")
    try:
        cwd_res = cwd.resolve()
        if not cwd_res.is_dir():
            return Speculation(False, "working directory is not a directory")
    except OSError:
        return Speculation(False, "working directory does not resolve")

    tmp = tempfile.mkdtemp(prefix="blast-spec-")
    base = Path(tmp)
    try:
        upper = base / "upper"
        work = base / "work"
        merged = base / "merged"
        for d in (upper, work, merged):
            d.mkdir()

        # lowerdir is the REAL tree (read-only by overlay construction); all
        # writes land in `upper`. The command runs cd'd into `merged`.
        script = _overlay_script_lower(cwd_res, upper, work, merged, command)
        try:
            proc = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--mount", "--net",
                 "bash", "-c", script],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # Partial writes may exist in `upper`, but a half-run command's
            # diff is unreliable — report the timeout, emit no targets.
            return Speculation(False, "command exceeded the speculation timeout")

        created, modified, deleted, truncated = diff_upper(upper, cwd_res)
        return Speculation(
            ran=True,
            reason="observed in overlay sandbox",
            created=created,
            modified=modified,
            deleted=deleted,
            exit_code=proc.returncode,
            truncated=truncated,
        )
    except OSError as exc:
        logger.debug("speculation failed: %s", exc, exc_info=True)
        return Speculation(False, "sandbox setup failed")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _overlay_script(base: Path, command: str) -> str:
    """Overlay script for the capability probe (lower is an empty scratch dir)."""
    return _overlay_script_lower(
        base / "lower", base / "upper", base / "work", base / "merged", command
    )


def _overlay_script_lower(
    lower: Path, upper: Path, work: Path, merged: Path, command: str
) -> str:
    """The bash run inside the namespace: mount overlay, cd in, run command.

    ``set -e`` up to the mount so a mount failure aborts with nonzero; the
    user command itself must NOT abort the script (we want its exit code, and
    a failing command still produced a real diff), so it runs after ``set +e``.

    The mount is attempted with ``userxattr`` first, falling back to a plain
    overlay. ``userxattr`` makes overlayfs record whiteouts as a ``user.overlay``
    xattr instead of a ``mknod`` character device — and device-node creation is
    forbidden inside the unprivileged user namespace this runs in. Without it,
    removing a *directory* from the lower layer fails with EIO on kernels that
    enforce that rule (WSL2, rootless containers, hardened hosts), so the
    flagship ``rm -rf ./somedir`` case would observe *nothing*. Kernels that
    don't support ``userxattr`` fall back to the char-device form.
    """
    q = _shq
    base = (
        f"mount -t overlay overlay -o "
        f"lowerdir={q(str(lower))},upperdir={q(str(upper))},workdir={q(str(work))}"
    )
    return (
        "set -e\n"
        f"{base},userxattr {q(str(merged))} 2>/dev/null || "
        f"{base} {q(str(merged))}\n"
        f"cd {q(str(merged))}\n"
        "set +e\n"
        f"{command}\n"
    )


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in the bash script."""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Diff parser (pure — the testable core)
# ---------------------------------------------------------------------------


def diff_upper(
    upper: Path, lower: Path
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    """Classify every entry in an overlay upperdir into created/modified/deleted.

    overlayfs records changes in the upper layer with a specific vocabulary:

    - a **whiteout** (character device, ``rdev == 0``) marks a path deleted
      from the lower layer;
    - a regular file/dir present in upper is **modified** if the same path
      exists in lower, else **created**.

    Args:
        upper: The overlay upperdir to walk.
        lower: The lowerdir (the real tree) — consulted only to tell created
            from modified. Never written.

    Returns:
        ``(created, modified, deleted, truncated)`` — path tuples relative to
        the tree, and a flag set when the walk hit the entry cap.

    Example::

        >>> diff_upper(Path("/scratch/upper"), Path("/proj"))
        (('new.txt',), ('config.py',), ('secret.env',), False)
    """
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    seen = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(upper):
        for name in list(dirnames) + filenames:
            seen += 1
            if seen > _MAX_WALK_ENTRIES or len(created) + len(modified) + len(deleted) >= _MAX_DIFF_ENTRIES:
                truncated = True
                break
            full = Path(dirpath) / name
            rel = str(full.relative_to(upper)).replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except OSError:
                continue
            kind = classify_upper_entry(
                st, (lower / rel).exists(), _has_whiteout_xattr(full)
            )
            if kind == "deleted":
                deleted.append(rel)
            elif kind == "created":
                created.append(rel)
            elif kind == "modified":
                modified.append(rel)
        if truncated:
            break

    return (
        tuple(sorted(set(created))),
        tuple(sorted(set(modified))),
        tuple(sorted(set(deleted))),
        truncated,
    )


def _has_whiteout_xattr(path: Path) -> bool:
    """True if ``path`` carries the ``user.overlay.whiteout`` xattr.

    ``userxattr``-mode overlays record a deletion as a 0-byte regular file
    tagged with this xattr instead of a char device. Linux-only; returns False
    where ``os.getxattr`` is unavailable (e.g. the Windows dev box, which never
    walks a real overlay).
    """
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return False
    try:
        getxattr(path, "user.overlay.whiteout", follow_symlinks=False)
        return True
    except OSError:
        return False


def classify_upper_entry(
    st: os.stat_result, exists_in_lower: bool, whiteout_xattr: bool = False
) -> str:
    """Classify one upperdir entry from its lstat + lower-layer presence.

    Pure and platform-agnostic (takes a stat result, does no I/O) so the
    overlay vocabulary can be unit-tested without root or a real overlay.
    ``whiteout_xattr`` flags the ``userxattr`` whiteout form (a 0-byte file
    carrying ``user.overlay.whiteout``); the char-device form is detected from
    ``st`` directly.

    Returns ``"deleted"`` (whiteout), ``"created"``, ``"modified"``, or
    ``"skip"`` (directories that merely exist in both layers — a passthrough,
    not a change).

    Example::

        >>> import os, stat
        >>> # a whiteout is a char device with rdev 0
        >>> classify_upper_entry(_fake(stat.S_IFCHR, 0), True)
        'deleted'
    """
    mode = st.st_mode
    # Whiteout: char device with device number 0 (default), or a file tagged
    # with the user.overlay.whiteout xattr (userxattr mode).
    if whiteout_xattr:
        return "deleted"
    if stat.S_ISCHR(mode) and st.st_rdev == 0:
        return "deleted"
    if stat.S_ISDIR(mode):
        # A directory in upper that also exists in lower is just a merge
        # passthrough (its changed children are walked separately). A brand
        # new directory is a creation worth reporting.
        return "skip" if exists_in_lower else "created"
    return "modified" if exists_in_lower else "created"
