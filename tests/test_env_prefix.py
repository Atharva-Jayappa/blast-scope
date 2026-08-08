"""Regression: env-assignment / exec-wrapper prefixes must not lower the score.

A leading ``FOO=bar`` (or ``env FOO=bar``, ``sudo``, …) is transparent — the
kernel runs the same command. The main parser peeled these prefixes, but the
command-class analyzers (git/docker/sql/packages/find/rsync) re-tokenized the
raw string and matched the verb positionally at ``tokens[0]``, so a prefix
silently disengaged the whole oracle layer and collapsed the score
(``FOO=bar git clean -fdx`` → LOW instead of CRITICAL). Worse than a wrong
number: the hook only snapshots on critical, so the prefix also disabled the
undo net. Every class now tokenizes on :func:`peeled_tokens`; this pins that a
prefix scores identically to the bare command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blast_scope.classes.packages import PackagesClass
from blast_scope.classes.sql import _extract_sql
from blast_scope.command_parser import _verb_basename, parse_command, peeled_tokens
from blast_scope.recoverability import clear_cache
from blast_scope.server import assess, reset_resolvers

PREFIXES = ["FOO=bar ", "env FOO=bar ", "FOO=bar BAR=baz ", "sudo "]


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()
    yield
    reset_resolvers()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()

    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(r), *a], capture_output=True, check=True)

    g("init")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    (r / "app.py").write_text("x = 1\n")
    g("add", "-A")
    g("commit", "-m", "init")
    (r / ".env").write_text("KEY=irreplaceable\n")  # untracked secret → critical
    return r


class TestPeeledTokens:
    def test_strips_assignments_and_wrappers(self) -> None:
        assert peeled_tokens("FOO=bar git clean -fdx") == ["git", "clean", "-fdx"]
        assert peeled_tokens("env FOO=bar sqlite3 a.db 'DROP TABLE t'") == [
            "sqlite3", "a.db", "DROP TABLE t"]
        assert peeled_tokens("sudo docker volume rm data") == ["docker", "volume", "rm", "data"]

    def test_keeps_path_verb_token(self) -> None:
        # peeled_tokens peels the *prefix* but does not basename the verb —
        # that is the verb-consumer's job (see TestPathVerbParity for the
        # end-to-end guarantee).
        assert peeled_tokens("FOO=1 /usr/bin/git clean -fdx")[0] == "/usr/bin/git"

    def test_only_prefix_is_empty(self) -> None:
        assert peeled_tokens("FOO=bar") == []


class TestPathVerbParity:
    """An absolute/relative path to the verb must score like the bare verb.

    Sibling of the prefix bug: `parsed["command"]` kept the full path
    (`/usr/bin/git`), so the class-triage gate (`command == "git"`) and the
    command-effect table both disengaged and `/bin/rm -rf x` scored LOW.
    """

    def test_verb_basename(self) -> None:
        assert _verb_basename("/usr/bin/git") == "git"
        assert _verb_basename("./rm") == "rm"
        assert _verb_basename("git") == "git"

    def test_path_git_stays_critical(self, repo: Path) -> None:
        bare = assess("git clean -fdx", cwd=str(repo), project_root=str(repo), auto_index=False)
        for verb in ["/usr/bin/git", "FOO=bar /usr/bin/git", "env /usr/bin/git"]:
            got = assess(f"{verb} clean -fdx", cwd=str(repo), project_root=str(repo), auto_index=False)
            assert got["severity"] == bare["severity"], f"{verb!r} changed severity"
            assert got["score"] == pytest.approx(bare["score"]), f"{verb!r} changed score"

    def test_path_rm_matches_bare(self, repo: Path) -> None:
        bare = assess("rm -rf .env", cwd=str(repo), project_root=str(repo), auto_index=False)
        got = assess("/bin/rm -rf .env", cwd=str(repo), project_root=str(repo), auto_index=False)
        assert got["severity"] == bare["severity"]
        assert got["score"] == pytest.approx(bare["score"])


class TestGitPrefixParity:
    def test_clean_stays_critical_under_prefix(self, repo: Path) -> None:
        bare = assess("git clean -fdx", cwd=str(repo), project_root=str(repo), auto_index=False)
        assert bare["severity"] == "critical"
        for p in PREFIXES:
            got = assess(p + "git clean -fdx", cwd=str(repo), project_root=str(repo), auto_index=False)
            assert got["severity"] == bare["severity"], f"{p!r} changed severity"
            assert got["score"] == pytest.approx(bare["score"]), f"{p!r} changed score"


class TestSqlPrefixParity:
    def test_sqlite_operands_survive_prefix(self) -> None:
        bare = _extract_sql("sqlite3 app.db 'DROP TABLE users'", "sqlite")
        assert bare == ("DROP TABLE users", "app.db")
        for p in PREFIXES:
            assert _extract_sql(p + "sqlite3 app.db 'DROP TABLE users'", "sqlite") == bare


class TestPackagesPrefixParity:
    def test_uninstall_triage_survives_prefix(self) -> None:
        for verb, op in [("uv pip uninstall requests", "uv_uninstall"),
                         ("pip uninstall flask", "pip_uninstall")]:
            assert PackagesClass().triage(verb, parse_command(verb)).operation == op
            for p in PREFIXES:
                raw = p + verb
                cand = PackagesClass().triage(raw, parse_command(raw))
                assert cand is not None and cand.operation == op, f"{raw!r} lost triage"


class TestFindPrefixParity:
    def test_delete_stays_high_under_prefix(self, repo: Path) -> None:
        # a tracked-dirty match makes the deletion consequential
        (repo / "app.log").write_text("data\n")
        subprocess.run(["git", "-C", str(repo), "add", "app.log"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "log"], capture_output=True)
        (repo / "app.log").write_text("changed\n")
        cmd = "find . -name '*.log' -delete"
        bare = assess(cmd, cwd=str(repo), project_root=str(repo), auto_index=False)
        for p in PREFIXES:
            got = assess(p + cmd, cwd=str(repo), project_root=str(repo), auto_index=False)
            assert got["score"] == pytest.approx(bare["score"]), f"{p!r} changed find score"
