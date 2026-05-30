"""Tests for blast_scope.recoverability."""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope.recoverability import classify_path, clear_cache


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


class TestRecoverability:
    def test_absent_path_is_low(self, tmp_path: Path) -> None:
        r = classify_path(tmp_path / "does_not_exist.txt")
        assert r["category"] == "absent"
        assert r["irrecoverability"] == 0.0

    def test_regenerable_dir_is_low(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("//")
        r = classify_path(nm / "index.js")
        assert r["category"] == "regenerable"
        assert r["reversible"] is True
        assert r["irrecoverability"] < 0.2

    def test_secret_file_is_unrecoverable(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("API_KEY=xyz")
        r = classify_path(env)
        assert r["category"] == "secret"
        assert r["reversible"] is False
        assert r["irrecoverability"] >= 0.9

    def test_pem_is_secret(self, tmp_path: Path) -> None:
        pem = tmp_path / "server.pem"
        pem.write_text("-----BEGIN-----")
        assert classify_path(pem)["category"] == "secret"

    def test_precious_data(self, tmp_path: Path) -> None:
        tf = tmp_path / "terraform.tfstate"
        tf.write_text("{}")
        r = classify_path(tf)
        assert r["category"] == "precious_data"
        assert r["irrecoverability"] >= 0.85

    def test_plain_untracked_file(self, tmp_path: Path) -> None:
        # tmp_path is not a git repo → untracked, not recoverable from history.
        f = tmp_path / "scratch.txt"
        f.write_text("data")
        r = classify_path(f)
        assert r["category"] == "untracked"
        assert r["reversible"] is False
