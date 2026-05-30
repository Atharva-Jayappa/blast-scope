"""Tests for blast_scope.command_parser."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from blast_scope.command_parser import (
    ParsedCommand,
    parse_command,
    _check_recursive,
    _classify_intent,
    _looks_like_path,
    _tokenize,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------


class TestBasicParsing:
    def test_rm_rf(self, tmp_path: Path) -> None:
        result = parse_command("rm -rf ./config", cwd=tmp_path)
        assert result["command"] == "rm"
        assert result["flags"] == ["-rf"]
        assert result["intent"] == "destructive"
        assert result["recursive"] is True
        assert len(result["targets"]) == 1
        assert result["targets"][0] == str((tmp_path / "config").resolve())

    def test_cat(self, tmp_path: Path) -> None:
        result = parse_command("cat main.py", cwd=tmp_path)
        assert result["command"] == "cat"
        assert result["intent"] == "read"
        assert result["recursive"] is False

    def test_mkdir(self, tmp_path: Path) -> None:
        result = parse_command("mkdir -p ./new_dir", cwd=tmp_path)
        assert result["command"] == "mkdir"
        assert result["intent"] == "additive"
        assert result["flags"] == ["-p"]

    def test_mv_is_destructive(self, tmp_path: Path) -> None:
        result = parse_command("mv a.py b.py", cwd=tmp_path)
        assert result["command"] == "mv"
        assert result["intent"] == "destructive"

    def test_chmod_is_destructive(self, tmp_path: Path) -> None:
        result = parse_command("chmod 755 script.sh", cwd=tmp_path)
        assert result["command"] == "chmod"
        assert result["intent"] == "destructive"

    def test_git_push_is_additive(self, tmp_path: Path) -> None:
        # A plain push adds commits to a remote — low risk, not destructive.
        result = parse_command("git push origin main", cwd=tmp_path)
        assert result["command"] == "git"
        assert result["intent"] == "additive"

    def test_git_force_push_is_destructive(self, tmp_path: Path) -> None:
        result = parse_command("git push --force origin main", cwd=tmp_path)
        assert result["command"] == "git"
        assert result["intent"] == "destructive"

    def test_git_reset_hard_is_destructive(self, tmp_path: Path) -> None:
        result = parse_command("git reset --hard HEAD~1", cwd=tmp_path)
        assert result["intent"] == "destructive"

    def test_git_status_is_read(self, tmp_path: Path) -> None:
        result = parse_command("git status", cwd=tmp_path)
        assert result["intent"] == "read"


# ---------------------------------------------------------------------------
# Sudo stripping
# ---------------------------------------------------------------------------


class TestSudoHandling:
    def test_sudo_rm(self, tmp_path: Path) -> None:
        result = parse_command("sudo rm -rf /etc", cwd=tmp_path)
        assert result["command"] == "rm"
        assert result["intent"] == "destructive"
        assert result["recursive"] is True

    def test_sudo_with_flags(self, tmp_path: Path) -> None:
        result = parse_command("sudo -u root rm file.txt", cwd=tmp_path)
        assert result["command"] == "rm"
        assert result["intent"] == "destructive"

    def test_bare_sudo(self, tmp_path: Path) -> None:
        result = parse_command("sudo", cwd=tmp_path)
        assert result["command"] == ""
        assert result["intent"] == "unknown"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_relative_path(self, tmp_path: Path) -> None:
        result = parse_command("rm ./config", cwd=tmp_path)
        assert str((tmp_path / "config").resolve()) in result["targets"]

    def test_absolute_path(self, tmp_path: Path) -> None:
        result = parse_command("rm /etc/passwd", cwd=tmp_path)
        assert any("/etc/passwd" in t or "\\etc\\passwd" in t for t in result["targets"])

    def test_multiple_targets(self, tmp_path: Path) -> None:
        result = parse_command("rm file1.py file2.py", cwd=tmp_path)
        assert len(result["targets"]) == 2


# ---------------------------------------------------------------------------
# Flag extraction
# ---------------------------------------------------------------------------


class TestFlagExtraction:
    def test_combined_short_flags(self) -> None:
        assert _check_recursive(["-rf"]) is True

    def test_separate_flags(self) -> None:
        assert _check_recursive(["--force", "--recursive"]) is True

    def test_no_recursive(self) -> None:
        assert _check_recursive(["--force"]) is False

    def test_capital_r(self) -> None:
        assert _check_recursive(["-R"]) is True

    def test_double_dash_separator(self, tmp_path: Path) -> None:
        result = parse_command("rm -- -rf", cwd=tmp_path)
        # After --, "-rf" is a positional argument, not a flag
        assert result["flags"] == []
        assert result["recursive"] is False


# ---------------------------------------------------------------------------
# Redirect handling
# ---------------------------------------------------------------------------


class TestRedirects:
    def test_output_redirect(self, tmp_path: Path) -> None:
        result = parse_command("echo hello > output.txt", cwd=tmp_path)
        assert any("output.txt" in t for t in result["targets"])

    def test_append_redirect(self, tmp_path: Path) -> None:
        result = parse_command("echo hello >> log.txt", cwd=tmp_path)
        assert any("log.txt" in t for t in result["targets"])


# ---------------------------------------------------------------------------
# Subshell detection
# ---------------------------------------------------------------------------


class TestSubshellDetection:
    def test_dollar_paren(self, tmp_path: Path) -> None:
        result = parse_command("rm $(find . -name '*.tmp')", cwd=tmp_path)
        assert result["intent"] == "unknown"

    def test_backticks(self, tmp_path: Path) -> None:
        result = parse_command("rm `find . -name '*.tmp'`", cwd=tmp_path)
        assert result["intent"] == "unknown"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string(self) -> None:
        result = parse_command("")
        assert result["command"] == ""
        assert result["intent"] == "unknown"
        assert result["targets"] == []

    def test_whitespace_only(self) -> None:
        result = parse_command("   ")
        assert result["command"] == ""

    def test_malformed_quotes(self, tmp_path: Path) -> None:
        # shlex should fail; we fall back to whitespace split
        result = parse_command("rm 'unterminated", cwd=tmp_path)
        assert result["command"] == "rm"

    def test_unknown_command(self, tmp_path: Path) -> None:
        result = parse_command("mycustomtool --do-stuff", cwd=tmp_path)
        assert result["command"] == "mycustomtool"
        assert result["intent"] == "unknown"


# ---------------------------------------------------------------------------
# Reversibility (mocked)
# ---------------------------------------------------------------------------


class TestReversibility:
    @patch("blast_scope.command_parser._check_reversibility", return_value=True)
    def test_reversible_target(self, mock_rev: object, tmp_path: Path) -> None:
        result = parse_command("rm file.py", cwd=tmp_path)
        assert result["reversible"] is True

    @patch("blast_scope.command_parser._check_reversibility", return_value=False)
    def test_not_reversible(self, mock_rev: object, tmp_path: Path) -> None:
        result = parse_command("rm file.py", cwd=tmp_path)
        assert result["reversible"] is False

    def test_no_targets_not_reversible(self) -> None:
        result = parse_command("")
        assert result["reversible"] is False


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    def test_destructive(self) -> None:
        for cmd in ("rm", "rmdir", "truncate", "dd", "mkfs", "shred"):
            assert _classify_intent(cmd, False) == "destructive"

    def test_additive(self) -> None:
        for cmd in ("touch", "mkdir", "cp", "tee"):
            assert _classify_intent(cmd, False) == "additive"

    def test_read(self) -> None:
        for cmd in ("cat", "head", "tail", "less", "grep", "find", "ls", "wc"):
            assert _classify_intent(cmd, False) == "read"

    def test_subshell_overrides(self) -> None:
        assert _classify_intent("rm", True) == "unknown"


# ---------------------------------------------------------------------------
# Fixture-driven tests
# ---------------------------------------------------------------------------


class TestFixtureDriven:
    @pytest.mark.parametrize(
        "command",
        [
            line.strip()
            for line in (FIXTURES_DIR / "destructive_commands.txt").read_text().splitlines()
            if line.strip()
        ],
    )
    def test_destructive_fixtures(self, command: str, tmp_path: Path) -> None:
        result = parse_command(command, cwd=tmp_path)
        assert result["intent"] == "destructive", f"Expected destructive for: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            line.strip()
            for line in (FIXTURES_DIR / "additive_commands.txt").read_text().splitlines()
            if line.strip()
        ],
    )
    def test_additive_fixtures(self, command: str, tmp_path: Path) -> None:
        result = parse_command(command, cwd=tmp_path)
        assert result["intent"] == "additive", f"Expected additive for: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            line.strip()
            for line in (FIXTURES_DIR / "read_commands.txt").read_text().splitlines()
            if line.strip()
        ],
    )
    def test_read_fixtures(self, command: str, tmp_path: Path) -> None:
        result = parse_command(command, cwd=tmp_path)
        assert result["intent"] == "read", f"Expected read for: {command}"


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic(self) -> None:
        assert _tokenize("rm -rf ./config") == ["rm", "-rf", "./config"]

    def test_quoted_args(self) -> None:
        assert _tokenize('grep "hello world" file.txt') == ["grep", "hello world", "file.txt"]

    def test_fallback_on_malformed(self) -> None:
        # Unterminated quote — should fall back to whitespace split
        result = _tokenize("rm 'unterminated")
        assert "rm" in result


class TestLooksLikePath:
    def test_relative_dot(self) -> None:
        assert _looks_like_path("./config") is True

    def test_absolute(self) -> None:
        assert _looks_like_path("/etc/passwd") is True

    def test_file_extension(self) -> None:
        assert _looks_like_path("main.py") is True

    def test_bare_word(self) -> None:
        # Bare words are treated as potential paths (intentionally broad)
        assert _looks_like_path("config") is True

    def test_flag_not_path(self) -> None:
        assert _looks_like_path("-rf") is False

    def test_special_chars(self) -> None:
        assert _looks_like_path("foo|bar") is False
