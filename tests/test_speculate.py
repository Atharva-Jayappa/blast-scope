"""Tests for speculative execution (speculate.py).

The pure logic — speculability gating and overlay-diff classification — runs on
every platform. The real overlay sandbox runs only where it is available
(Linux + unprivileged namespaces), and the marquee test there is the safety
invariant: after a destructive run in the sandbox, the REAL tree is untouched.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from blast_scope import speculate
from blast_scope.speculate import (
    Speculation,
    classify_upper_entry,
    diff_upper,
    is_speculable,
)


class _FakeStat:
    """Minimal stand-in for os.stat_result (mode + rdev only)."""

    def __init__(self, mode: int, rdev: int = 0) -> None:
        self.st_mode = mode
        self.st_rdev = rdev


# ---------------------------------------------------------------------------
# Speculability gate (pure, every platform)
# ---------------------------------------------------------------------------


class TestSpeculability:
    def _seg(self, command: str, targets: list[str] | None = None) -> dict:
        return {"command": command, "targets": targets or [], "write_targets": targets or []}

    def test_plain_rm_is_speculable(self, tmp_path: Path) -> None:
        v = is_speculable("rm -rf build", [self._seg("rm", ["build"])], tmp_path)
        assert v.ok

    @pytest.mark.parametrize("verb", ["curl", "wget", "ssh", "aws", "nc"])
    def test_network_verbs_refused(self, verb: str, tmp_path: Path) -> None:
        v = is_speculable(f"{verb} x", [self._seg(verb)], tmp_path)
        assert not v.ok and v.rejected_by == verb

    @pytest.mark.parametrize("verb", ["git", "docker", "npm", "sudo", "systemctl", "dd"])
    def test_external_state_and_privilege_refused(self, verb: str, tmp_path: Path) -> None:
        v = is_speculable(f"{verb} x", [self._seg(verb)], tmp_path)
        assert not v.ok

    def test_absolute_path_outside_cwd_refused(self, tmp_path: Path) -> None:
        v = is_speculable(
            "rm -rf /etc/nginx", [self._seg("rm", ["/etc/nginx"])], tmp_path
        )
        assert not v.ok
        assert "outside" in v.reason

    def test_absolute_path_inside_cwd_allowed(self, tmp_path: Path) -> None:
        inside = str(tmp_path / "sub" / "f")
        v = is_speculable(f"rm {inside}", [self._seg("rm", [inside])], tmp_path)
        assert v.ok

    def test_relative_target_allowed(self, tmp_path: Path) -> None:
        v = is_speculable("rm ./data/x", [self._seg("rm", ["data/x"])], tmp_path)
        assert v.ok

    def test_unresolved_substitution_refused(self, tmp_path: Path) -> None:
        v = is_speculable("rm -rf $(cat list)", [self._seg("rm")], tmp_path)
        assert not v.ok
        assert "black box" in v.reason

    def test_chain_refused_if_any_segment_unsafe(self, tmp_path: Path) -> None:
        segs = [self._seg("rm", ["build"]), self._seg("curl")]
        v = is_speculable("rm -rf build && curl x", segs, tmp_path)
        assert not v.ok


# ---------------------------------------------------------------------------
# Overlay diff vocabulary (pure)
# ---------------------------------------------------------------------------


class TestClassifyUpperEntry:
    def test_whiteout_is_deleted(self) -> None:
        # overlayfs whiteout: char device, rdev 0.
        st = _FakeStat(stat.S_IFCHR, rdev=0)
        assert classify_upper_entry(st, exists_in_lower=True) == "deleted"

    def test_char_device_nonzero_rdev_not_whiteout(self) -> None:
        # A real char device (rdev != 0) is NOT a whiteout — must not read as
        # a deletion. Present in lower ⇒ modified, proving it isn't "deleted".
        st = _FakeStat(stat.S_IFCHR, rdev=42)
        assert classify_upper_entry(st, exists_in_lower=True) == "modified"

    def test_regular_file_in_lower_is_modified(self) -> None:
        st = _FakeStat(stat.S_IFREG)
        assert classify_upper_entry(st, exists_in_lower=True) == "modified"

    def test_regular_file_new_is_created(self) -> None:
        st = _FakeStat(stat.S_IFREG)
        assert classify_upper_entry(st, exists_in_lower=False) == "created"

    def test_existing_dir_is_passthrough(self) -> None:
        st = _FakeStat(stat.S_IFDIR)
        assert classify_upper_entry(st, exists_in_lower=True) == "skip"

    def test_new_dir_is_created(self) -> None:
        st = _FakeStat(stat.S_IFDIR)
        assert classify_upper_entry(st, exists_in_lower=False) == "created"


class TestDiffUpper:
    """diff_upper against a hand-built upperdir (created/modified only — a real
    whiteout needs mknod/root, exercised in the Linux sandbox test)."""

    def test_created_and_modified_split_by_lower(self, tmp_path: Path) -> None:
        lower = tmp_path / "lower"
        upper = tmp_path / "upper"
        lower.mkdir()
        upper.mkdir()
        (lower / "existing.py").write_text("old")
        (upper / "existing.py").write_text("new")   # modified
        (upper / "brand_new.txt").write_text("x")   # created
        created, modified, deleted, trunc = diff_upper(upper, lower)
        assert created == ("brand_new.txt",)
        assert modified == ("existing.py",)
        assert deleted == ()
        assert trunc is False

    def test_nested_paths_relative(self, tmp_path: Path) -> None:
        lower = tmp_path / "lower"
        upper = tmp_path / "upper"
        (upper / "a" / "b").mkdir(parents=True)
        lower.mkdir()
        (upper / "a" / "b" / "deep.txt").write_text("x")
        created, _, _, _ = diff_upper(upper, lower)
        assert "a/b/deep.txt" in created


# ---------------------------------------------------------------------------
# Availability + graceful degradation (every platform)
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_speculate_unavailable_returns_not_ran(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(speculate, "available", lambda: False)
        r = speculate.speculate("rm -rf x", tmp_path)
        assert r.ran is False
        assert "unavailable" in r.reason
        assert r.destroyed == ()

    def test_speculation_dataclass_helpers(self) -> None:
        s = Speculation(
            ran=True, reason="observed", created=("a",), modified=("b",), deleted=("c",)
        )
        assert set(s.touched) == {"a", "b", "c"}
        assert set(s.destroyed) == {"b", "c"}  # modified + deleted, not created


# ---------------------------------------------------------------------------
# Real overlay sandbox — Linux only. The safety invariant lives here.
# ---------------------------------------------------------------------------

needs_sandbox = pytest.mark.skipif(
    not speculate.available(), reason="overlay sandbox unavailable (non-Linux / no userns)"
)


@needs_sandbox
class TestRealSandbox:
    def test_deletion_observed_and_real_tree_untouched(self, tmp_path: Path) -> None:
        # The safety invariant: run a destructive command in the sandbox and
        # assert (a) we observed the deletion, (b) the REAL file still exists.
        victim = tmp_path / "important.txt"
        victim.write_text("do not lose me")
        result = speculate.speculate("rm important.txt", tmp_path)
        assert result.ran
        assert "important.txt" in result.deleted
        # THE critical assertion — the real filesystem was never touched.
        assert victim.exists()
        assert victim.read_text() == "do not lose me"

    def test_creation_and_modification_observed(self, tmp_path: Path) -> None:
        (tmp_path / "existing.txt").write_text("v1")
        result = speculate.speculate(
            "echo v2 > existing.txt && echo new > created.txt", tmp_path
        )
        assert result.ran
        assert "existing.txt" in result.modified
        assert "created.txt" in result.created
        # Real tree unchanged.
        assert (tmp_path / "existing.txt").read_text() == "v1"
        assert not (tmp_path / "created.txt").exists()

    def test_recursive_delete_lists_tree(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        (tmp_path / "src" / "b.py").write_text("y")
        result = speculate.speculate("rm -rf src", tmp_path)
        assert result.ran
        assert any("a.py" in d for d in result.deleted)
        assert (tmp_path / "src" / "a.py").exists()  # real tree intact
