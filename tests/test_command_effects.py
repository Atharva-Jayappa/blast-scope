"""Tests for command effect classification and Windows/PowerShell parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope.command_effects import classify_effect, canonicalize
from blast_scope.command_parser import parse_command


class TestFlagSensitiveClassification:
    def test_find_plain_is_read(self) -> None:
        assert classify_effect("find", [], [".", "-name", "*.py"]).intent == "read"

    def test_find_delete_is_destructive(self) -> None:
        eff = classify_effect("find", ["-delete"], ["."])
        assert eff.intent == "destructive"
        assert eff.weight >= 0.7

    def test_find_exec_rm_is_destructive(self) -> None:
        assert classify_effect("find", ["-exec"], [".", "rm"]).intent == "destructive"

    def test_sed_plain_is_read(self) -> None:
        assert classify_effect("sed", [], ["s/a/b/", "f.py"]).intent == "read"

    def test_sed_inplace_is_destructive(self) -> None:
        eff = classify_effect("sed", ["-i"], ["s/a/b/", "f.py"])
        assert eff.intent == "destructive"
        assert eff.in_place is True

    def test_dd_of_is_critical_weight(self) -> None:
        assert classify_effect("dd", [], ["if=/dev/zero", "of=/dev/sda"]).weight == 1.0

    def test_redirect_clobber_overwrites(self) -> None:
        eff = classify_effect("echo", [], ["hi"], clobber=True)
        assert eff.intent == "destructive"


class TestGitSubcommands:
    def test_reset_hard(self) -> None:
        assert classify_effect("git", ["--hard"], ["reset", "HEAD~1"]).intent == "destructive"

    def test_clean_force(self) -> None:
        assert classify_effect("git", ["-fdx"], ["clean"]).intent == "destructive"

    def test_force_push(self) -> None:
        assert classify_effect("git", ["--force"], ["push"]).intent == "destructive"

    def test_status_is_read(self) -> None:
        assert classify_effect("git", [], ["status"]).intent == "read"


class TestWeights:
    def test_read_is_zero(self) -> None:
        assert classify_effect("cat", [], []).weight == 0.0

    def test_recursive_chmod_escalates(self) -> None:
        base = classify_effect("chmod", [], ["file"]).weight
        rec = classify_effect("chmod", ["-R"], ["dir"]).weight
        assert rec > base

    def test_unknown_destructive_fallback(self) -> None:
        # find isn't in the weight table; a destructive find still gets weight.
        assert classify_effect("find", ["-delete"], ["."]).weight >= 0.5


class TestPowerShell:
    def test_canonicalize_remove_item(self) -> None:
        assert canonicalize("Remove-Item") == "rm"
        assert canonicalize("ri") == "rm"
        assert canonicalize("del") == "rm"

    def test_remove_item_parsed_as_rm(self, tmp_path: Path) -> None:
        result = parse_command("Remove-Item -Recurse -Force build", shell="powershell", cwd=tmp_path)
        assert result["command"] == "rm"
        assert result["intent"] == "destructive"
        assert result["recursive"] is True

    def test_powershell_preserves_backslash_path(self, tmp_path: Path) -> None:
        result = parse_command(r"Remove-Item .\dist", shell="powershell", cwd=tmp_path)
        assert result["command"] == "rm"
        # The dist path should be captured as a target (not mangled away).
        assert len(result["targets"]) == 1
