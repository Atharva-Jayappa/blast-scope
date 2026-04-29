"""Tests for chained command splitting, parsing, and scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope.command_parser import (
    parse_command_chain,
    split_command_chain,
)
from blast_scope.graph_resolver import GraphResolver
from blast_scope.risk_scorer import score_chain

SAMPLE_PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


# ---------------------------------------------------------------------------
# split_command_chain
# ---------------------------------------------------------------------------


class TestSplitChain:
    def test_empty(self) -> None:
        assert split_command_chain("") == []
        assert split_command_chain("   ") == []

    def test_single_command(self) -> None:
        assert split_command_chain("rm -rf /tmp") == ["rm -rf /tmp"]

    def test_double_amp(self) -> None:
        assert split_command_chain("cd /tmp && rm -rf .") == ["cd /tmp", "rm -rf ."]

    def test_double_pipe(self) -> None:
        assert split_command_chain("test -f x || rm x") == ["test -f x", "rm x"]

    def test_semicolon(self) -> None:
        assert split_command_chain("ls; rm a; rm b") == ["ls", "rm a", "rm b"]

    def test_pipe(self) -> None:
        assert split_command_chain("cat foo | grep bar") == ["cat foo", "grep bar"]

    def test_mixed_operators(self) -> None:
        assert split_command_chain("a && b || c ; d | e") == ["a", "b", "c", "d", "e"]

    def test_quoted_operator_not_split(self) -> None:
        # Semicolon inside quotes should not split
        assert split_command_chain("echo 'a; b'; ls") == ["echo 'a; b'", "ls"]

    def test_double_quoted_operator_not_split(self) -> None:
        assert split_command_chain('echo "x && y" && ls') == ['echo "x && y"', "ls"]

    def test_command_substitution_not_split(self) -> None:
        # $() contains its own &&, should not split there
        assert split_command_chain("rm $(find . && echo done) ; ls") == [
            "rm $(find . && echo done)",
            "ls",
        ]

    def test_backtick_substitution_not_split(self) -> None:
        assert split_command_chain("rm `find . ; echo`; ls") == [
            "rm `find . ; echo`",
            "ls",
        ]

    def test_escaped_operator(self) -> None:
        # Escaped semicolon is part of the segment, not an operator
        result = split_command_chain(r"echo a\; b ; ls")
        assert result == [r"echo a\; b", "ls"]

    def test_trailing_operator(self) -> None:
        assert split_command_chain("ls;") == ["ls"]

    def test_leading_operator(self) -> None:
        assert split_command_chain(";ls") == ["ls"]

    def test_repeated_operators(self) -> None:
        # ;;; collapses to empty segments which are dropped
        assert split_command_chain("ls;;rm x") == ["ls", "rm x"]


# ---------------------------------------------------------------------------
# parse_command_chain
# ---------------------------------------------------------------------------


class TestParseChain:
    def test_empty(self) -> None:
        assert parse_command_chain("") == []

    def test_single_command(self, tmp_path: Path) -> None:
        result = parse_command_chain("rm -rf ./config", cwd=tmp_path)
        assert len(result) == 1
        assert result[0]["command"] == "rm"
        assert result[0]["recursive"] is True

    def test_two_commands(self, tmp_path: Path) -> None:
        result = parse_command_chain("ls && rm file.py", cwd=tmp_path)
        assert len(result) == 2
        assert result[0]["command"] == "ls"
        assert result[1]["command"] == "rm"

    def test_cd_updates_cwd(self, tmp_path: Path) -> None:
        # Create a sub-directory we can cd into
        subdir = tmp_path / "sub"
        subdir.mkdir()

        # Use POSIX-style path so shlex doesn't eat Windows backslashes
        result = parse_command_chain(
            f"cd {subdir.as_posix()} && rm file.py", cwd=tmp_path
        )
        assert len(result) == 2
        assert result[0]["command"] == "cd"
        assert result[1]["command"] == "rm"

        # rm should resolve "file.py" against the new cwd (subdir), not tmp_path
        rm_targets = result[1]["targets"]
        assert len(rm_targets) == 1
        # Compare resolved paths; Windows uses \ in resolved output
        assert Path(rm_targets[0]).parent.resolve() == subdir.resolve()

    def test_cd_to_nonexistent_keeps_cwd(self, tmp_path: Path) -> None:
        # cd to a non-existent directory should NOT change cwd for following commands
        result = parse_command_chain(
            "cd /nonexistent_dir_xyz_12345 && rm file.py", cwd=tmp_path
        )
        assert len(result) == 2
        # rm should still resolve against tmp_path
        rm_targets = result[1]["targets"]
        assert Path(rm_targets[0]).parent.resolve() == tmp_path.resolve()


# ---------------------------------------------------------------------------
# score_chain
# ---------------------------------------------------------------------------


@pytest.fixture()
def resolver(tmp_path: Path) -> GraphResolver:
    db_path = tmp_path / "chain_graph.db"
    r = GraphResolver(SAMPLE_PROJECT, db_path=db_path)
    r.build_graph()
    return r


class TestScoreChain:
    def test_empty_chain(self) -> None:
        result = score_chain([], [])
        assert result["score"] == 0.0
        assert result["severity"] == "low"
        assert result["recommendation"] == "proceed"
        assert result["chain"] == []

    def test_single_command_chain(self, tmp_path: Path) -> None:
        parsed = parse_command_chain("cat file.py", cwd=tmp_path)
        result = score_chain(parsed, [[]])
        assert result["severity"] == "low"
        assert len(result["chain"]) == 1

    def test_chain_picks_worst_step(self, resolver: GraphResolver) -> None:
        """`ls && rm config.py` — the rm dominates the score."""
        parsed = parse_command_chain("ls && rm ./config.py", cwd=SAMPLE_PROJECT)
        resolutions_per_cmd = []
        for p in parsed:
            cmd_res = [resolver.resolve_path(Path(t)) for t in p["targets"]]
            resolutions_per_cmd.append(cmd_res)

        result = score_chain(parsed, resolutions_per_cmd)

        # Top-level score should equal the worst step's score
        assert len(result["chain"]) == 2
        ls_score = result["chain"][0]["assessment"]["score"]
        rm_score = result["chain"][1]["assessment"]["score"]
        assert result["score"] == max(ls_score, rm_score)
        # rm is destructive — should outweigh ls (read)
        assert rm_score > ls_score

    def test_chain_rationale_mentions_worst_step(self, resolver: GraphResolver) -> None:
        parsed = parse_command_chain("ls && rm ./config.py", cwd=SAMPLE_PROJECT)
        resolutions_per_cmd = []
        for p in parsed:
            cmd_res = [resolver.resolve_path(Path(t)) for t in p["targets"]]
            resolutions_per_cmd.append(cmd_res)

        result = score_chain(parsed, resolutions_per_cmd, raw_segments=["ls", "rm ./config.py"])

        assert "Chain of 2" in result["rationale"]
        assert "rm" in result["rationale"]

    def test_cd_then_rm_resolves_correctly(self, resolver: GraphResolver) -> None:
        """cd into sample_project then rm config.py — the rm should hit the graph."""
        # Use a cwd outside the project to verify cd updates it
        outside = SAMPLE_PROJECT.parent.parent  # tests/
        parsed = parse_command_chain(
            f"cd {SAMPLE_PROJECT} && rm config.py",
            cwd=outside,
        )
        resolutions_per_cmd = []
        for p in parsed:
            cmd_res = [resolver.resolve_path(Path(t)) for t in p["targets"]]
            resolutions_per_cmd.append(cmd_res)

        result = score_chain(parsed, resolutions_per_cmd)

        # The rm step should have found graph data
        rm_step = result["chain"][1]
        assert rm_step["parsed"]["command"] == "rm"
        # Score should be > 0 because config.py has importers
        assert rm_step["assessment"]["score"] > 0.0
