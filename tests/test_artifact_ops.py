import json
import tempfile
import textwrap
from pathlib import Path

from tests.helpers import LocalSDLCTestCase


class ArtifactOpsTests(LocalSDLCTestCase):
    def test_extract_file_artifact_from_begin_file(self):
        output = "BEGIN_FILE: index.html\n<!doctype html>\n<html></html>\nEND_FILE"
        artifact = self.local_sdlc.extract_file_artifact(output, ["index.html"])

        self.assertEqual(artifact.path, "index.html")
        self.assertEqual(artifact.mode, "replace")
        self.assertIn("<html>", artifact.content)

    def test_extract_file_artifact_accepts_path_on_next_line(self):
        output = "BEGIN_FILE\nindex.html\n<!doctype html>\n<html></html>\nEND_FILE"
        artifact = self.local_sdlc.extract_file_artifact(output, ["index.html"])

        self.assertEqual(artifact.path, "index.html")
        self.assertIn("<html>", artifact.content)

    def test_extract_file_artifact_accepts_path_label_on_next_line(self):
        output = "BEGIN_FILE\npath: index.html\n<!doctype html>\n<html></html>\nEND_FILE"
        artifact = self.local_sdlc.extract_file_artifact(output, ["index.html"])

        self.assertEqual(artifact.path, "index.html")
        self.assertIn("<html>", artifact.content)

    def test_extract_file_artifact_accepts_path_equals_and_separator_lines(self):
        output = "BEGIN_FILE\npath=app.py\n---\nprint('ok')\n---\nEND_FILE"
        artifact = self.local_sdlc.extract_file_artifact(output, ["app.py"])

        self.assertEqual(artifact.path, "app.py")
        self.assertEqual(artifact.content, "print('ok')")

    def test_extract_file_artifact_salvages_missing_end_marker(self):
        output = "BEGIN_FILE: resp.py\nprint('parser')\n"
        artifact = self.local_sdlc.extract_file_artifact(output, ["resp.py"])

        self.assertEqual(artifact.path, "resp.py")
        self.assertIn("parser", artifact.content)

    def test_extract_file_artifact_salvages_premature_end_marker(self):
        output = "BEGIN_FILE: resp.py\nEND_FILE\nprint('parser')\n"
        artifact = self.local_sdlc.extract_file_artifact(output, ["resp.py"])

        self.assertEqual(artifact.path, "resp.py")
        self.assertIn("parser", artifact.content)

    def test_extract_file_artifact_rejects_empty_content(self):
        output = "BEGIN_FILE\nindex.html\nEND_FILE"

        with self.assertRaises(self.local_sdlc.RunnerError):
            self.local_sdlc.extract_file_artifact(output, ["index.html"])

    def test_extract_file_artifact_from_append_file(self):
        output = "BEGIN_APPEND_FILE: index.html\n```html\n</script>\n</html>\n```\nEND_APPEND_FILE"
        artifact = self.local_sdlc.extract_file_artifact(output, ["index.html"])

        self.assertEqual(artifact.path, "index.html")
        self.assertEqual(artifact.mode, "append")
        self.assertIn("</html>", artifact.content)
        self.assertNotIn("```", artifact.content)

    def test_extract_multiple_file_artifacts(self):
        output = """BEGIN_FILE: server.py
print("server")
END_FILE
BEGIN_FILE: README.md
# Redis mini server
END_FILE"""
        artifacts = self.local_sdlc.extract_file_artifacts(output, ["server.py", "README.md"])

        self.assertEqual([artifact.path for artifact in artifacts], ["server.py", "README.md"])
        self.assertIn('print("server")', artifacts[0].content)
        self.assertIn("# Redis mini server", artifacts[1].content)

    def test_extract_file_artifact_accepts_path_qualified_end_file(self):
        output = "BEGIN_FILE: app.py\nprint('ok')\nEND_FILE: app.py"

        artifacts = self.local_sdlc.extract_file_artifacts(output, ["app.py"])
        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)

        self.assertEqual([artifact.path for artifact in artifacts], ["app.py"])
        self.assertIn("print('ok')", artifacts[0].content)
        self.assertNotIn("unbalanced_file_artifact", {finding.code for finding in findings})
        self.assertNotIn("format_repair_no_artifact", {finding.code for finding in findings})

    def test_extract_file_artifact_recovers_malformed_search_replace_full_python_file(self):
        output = '''BEGIN_SEARCH_REPLACE: app.py
: app.py
"""Application module."""

from __future__ import annotations

import sys


def main():
    return sys.version


if __name__ == "__main__":
    main()
'''

        artifacts = self.local_sdlc.extract_file_artifacts(output, ["app.py"])

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].path, "app.py")
        self.assertEqual(artifacts[0].mode, "replace")
        self.assertIn("def main", artifacts[0].content)

    def test_extract_file_artifact_does_not_recover_malformed_search_replace_fragment(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
: app.py
def broken(
"""

        artifacts = self.local_sdlc.extract_file_artifacts(output, ["app.py"])

        self.assertEqual(artifacts, [])

    def test_extract_multiple_unclosed_file_artifacts(self):
        output = """BEGIN_FILE: server.py
print("server")
BEGIN_FILE: README.md
# Redis mini server
"""
        artifacts = self.local_sdlc.extract_file_artifacts(output, ["server.py", "README.md"])

        self.assertEqual([artifact.path for artifact in artifacts], ["server.py", "README.md"])
        self.assertIn('print("server")', artifacts[0].content)
        self.assertIn("# Redis mini server", artifacts[1].content)

    def test_extracts_path_headed_fenced_file_artifacts(self):
        output = """```python
# minisqlite/errors.py
class MiniSQLiteError(Exception):
    pass
```

```python
# tests/test_core.py
import unittest
```
"""
        artifacts = self.local_sdlc.extract_file_artifacts(
            output,
            ["minisqlite/errors.py", "tests/test_core.py"],
        )

        self.assertEqual([artifact.path for artifact in artifacts], ["minisqlite/errors.py", "tests/test_core.py"])
        self.assertIn("class MiniSQLiteError", artifacts[0].content)
        self.assertNotIn("minisqlite/errors.py", artifacts[0].content)

    def test_extract_file_artifact_allows_safe_extra_new_file(self):
        output = "BEGIN_FILE: minisqlite/connection.py\nVALUE = 'ok'\nEND_FILE"
        policy = self.local_sdlc.ArtifactPathPolicy(
            allowed_paths=("minisqlite/sql.py",),
            existing_paths=("SPEC.md", "minisqlite/sql.py"),
            allow_extra_new_files=True,
        )

        artifact = self.local_sdlc.extract_file_artifact(output, policy)

        self.assertEqual(artifact.path, "minisqlite/connection.py")
        self.assertIn("VALUE", artifact.content)

    def test_extract_file_artifact_rejects_existing_readonly_extra_file(self):
        output = "BEGIN_FILE: base.py\nVALUE = 'bad'\nEND_FILE"
        policy = self.local_sdlc.ArtifactPathPolicy(
            allowed_paths=("app.py",),
            readonly_paths=("base.py",),
            existing_paths=("app.py", "base.py"),
            allow_extra_new_files=True,
        )

        with self.assertRaisesRegex(self.local_sdlc.RunnerError, "read-only"):
            self.local_sdlc.extract_file_artifact(output, policy)

    def test_merge_artifact_policy_paths_promotes_readonly_to_writable(self):
        allowed, readonly = self.local_sdlc.merge_artifact_policy_paths(
            allowed_paths=["app.py"],
            readonly_paths=["minisqlite/sql/lexer.py", "tests/test_parser.py"],
            added_writable_paths=["minisqlite/sql/lexer.py"],
            added_readonly_paths=["minisqlite/sql/parser.py"],
        )

        self.assertEqual(allowed, ["app.py", "minisqlite/sql/lexer.py"])
        self.assertEqual(readonly, ["tests/test_parser.py", "minisqlite/sql/parser.py"])

    def test_extract_and_apply_search_replace_artifact(self):
        output = """BEGIN_SEARCH_REPLACE: index.html
<<<<<<< SEARCH
renderBoard();
=======
initBoard();
renderBoard();
>>>>>>> REPLACE
END_SEARCH_REPLACE"""
        artifact = self.local_sdlc.extract_search_replace_artifact(output, ["index.html"])

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "index.html").write_text("renderBoard();\n", encoding="utf-8")
            doc = self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)
            text = (project / "index.html").read_text(encoding="utf-8")

        self.assertIn("PASS", doc)
        self.assertEqual(text, "initBoard();\nrenderBoard();\n")

    def test_extract_fenced_search_replace_normalizes_extra_colon_path(self):
        output = """BEGIN_SEARCH_REPLACE: : tests/test_core.py
```python
<<<<<<< SEARCH
    def test_raise_wrong_type(self):
        with self.assertRaises(SQLSyntaxError):
            raise SchemaError("no table")
=======
    def test_raise_wrong_type(self):
        with self.assertRaises(SchemaError):
            raise SchemaError("no table")
>>>>>>> REPLACE
```
"""

        findings = self.local_sdlc.lint_artifact_output(output, [], [], format_repair_mode=True)
        artifact = self.local_sdlc.extract_search_replace_artifact(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("tests/test_core.py",), allow_extra_new_files=True),
        )

        self.assertEqual({finding.code for finding in findings}, set())
        self.assertEqual(artifact.path, "tests/test_core.py")
        self.assertNotEqual(artifact.search, "__PY_FUNCTION_REPLACE__:test_raise_wrong_type")
        self.assertIn("with self.assertRaises(SQLSyntaxError)", artifact.search)
        self.assertIn("with self.assertRaises(SchemaError)", artifact.replace)

    def test_apply_search_replace_falls_back_to_malformed_single_function_replacement(self):
        artifact = self.local_sdlc.SearchReplaceArtifact(
            path="app.py",
            search='def run(self):\nreturn "old"',
            replace='def run(self):\nif True:\n    return "new"',
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "app.py").write_text(
                "class App:\n"
                "    def run(self):\n"
                "        return \"old\"\n\n"
                "    def other(self):\n"
                "        return \"other\"\n",
                encoding="utf-8",
            )
            doc = self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)
            text = (project / "app.py").read_text(encoding="utf-8")

        self.assertIn("Python Function Replacement Result", doc)
        self.assertIn("    def run(self):\n        if True:\n            return \"new\"", text)
        self.assertIn("def other", text)
        compile(text, "app.py", "exec")

    def test_mixed_replace_file_artifact_paths_detects_same_path_only(self):
        paths = self.local_sdlc.mixed_replace_file_artifact_paths(
            [
                self.local_sdlc.SearchReplaceArtifact(
                    path="app.py",
                    search="old",
                    replace="new",
                )
            ],
            [
                self.local_sdlc.FileArtifact(path="app.py", content="x", mode="replace"),
                self.local_sdlc.FileArtifact(path="other.py", content="x", mode="replace"),
                self.local_sdlc.FileArtifact(path="app.py", content="x", mode="append"),
            ],
        )

        self.assertEqual(paths, ["app.py"])

    def test_json_artifacts_reject_duplicate_keys(self):
        output = """{"artifacts": [
  {
    "type": "search_replace",
    "path": "app.py",
    "search": "old",
    "replace": "new",
    "type": "search_replace",
    "path": "tests/test_app.py",
    "search": "x",
    "replace": "y"
  }
]}"""

        with self.assertRaisesRegex(self.local_sdlc.RunnerError, "duplicate JSON artifact key"):
            self.local_sdlc.extract_json_artifacts(
                output,
                self.local_sdlc.ArtifactPathPolicy(
                    allowed_paths=("app.py",),
                    readonly_paths=("tests/test_app.py",),
                    existing_paths=("app.py", "tests/test_app.py"),
                ),
            )

    def test_json_artifacts_repairs_trailing_commas(self):
        output = """{
  "artifacts": [
    {
      "type": "search_replace",
      "path": "app.py",
      "search": "old, still inside string",
      "replace": "new",
    },
  ],
}"""

        replacements, files = self.local_sdlc.extract_json_artifacts(output, ("app.py",))

        self.assertEqual(files, [])
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].path, "app.py")
        self.assertEqual(replacements[0].search, "old, still inside string")
        self.assertEqual(replacements[0].replace, "new")

    def test_multi_pair_search_replace_recovers_end_marker_typo(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
def one():
    return "old"
=======
def one():
    return "new"
>>>>>>> SEARCH

<<<<<<< SEARCH
def two():
    return "old"
=======
def two():
    return "new"
>>>>>>> REPLACE
"""

        artifacts = self.local_sdlc.extract_search_replace_artifacts(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), existing_paths=("app.py",)),
        )

        self.assertEqual(len(artifacts), 2)
        self.assertEqual(artifacts[0].path, "app.py")
        self.assertIn('return "new"', artifacts[0].replace)
        self.assertIn("def two", artifacts[1].search)

    def test_search_replace_recovers_extra_gt_end_marker(self):
        output = """BEGIN_SEARCH_REPLACE: app.py
<<<<<<< SEARCH
old
=======
new
>>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        artifacts = self.local_sdlc.extract_search_replace_artifacts(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("app.py",), existing_paths=("app.py",)),
        )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].path, "app.py")
        self.assertEqual(artifacts[0].search, "old")
        self.assertEqual(artifacts[0].replace, "new")

    def test_file_artifact_html_fallback_ignores_search_replace_protocol(self):
        output = """BEGIN_SEARCH_REPLACE: tetris.html
<<<<<<< SEARCH
old
=======
<!DOCTYPE html>
<html><body>new</body></html>
>>>>>>>> REPLACE
END_SEARCH_REPLACE"""

        files = self.local_sdlc.extract_file_artifacts(
            output,
            self.local_sdlc.ArtifactPathPolicy(allowed_paths=("tetris.html",), existing_paths=("tetris.html",)),
        )

        self.assertEqual(files, [])

    def test_salvage_completed_artifact_prefix_before_readonly_path(self):
        output = """BEGIN_FILE: app.py
```python
print("safe")
```
END_FILE

BEGIN_FILE: tests/test_app.py
```python
raise AssertionError("do not edit")
```
"""
        policy = self.local_sdlc.ArtifactPathPolicy(
            allowed_paths=("app.py",),
            readonly_paths=("tests/test_app.py",),
            existing_paths=("app.py", "tests/test_app.py"),
        )

        salvaged = self.local_sdlc.salvage_completed_artifact_prefix_before_readonly_path(output, policy)

        self.assertIsNotNone(salvaged)
        self.assertNotIn("tests/test_app.py", salvaged)
        files = self.local_sdlc.extract_file_artifacts(salvaged or "", policy)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "app.py")
        self.assertIn('print("safe")', files[0].content)

    def test_extract_json_file_and_search_replace_artifacts(self):
        output = json.dumps(
            {
                "artifacts": [
                    {"type": "replace_file", "path": "server.py", "content": "VALUE = 'ok'\n"},
                    {"type": "search_replace", "path": "README.md", "search": "old", "replace": "new"},
                ]
            }
        )

        replacements, files = self.local_sdlc.extract_json_artifacts(output, ["server.py", "README.md"])

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "server.py")
        self.assertEqual(files[0].mode, "replace")
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].search, "old")

    def test_extract_json_repairs_duplicated_type_key_value(self):
        output = (
            '{"artifacts":[{"type":"type":"search_replace","path":"app.py",'
            '"search":"old","replace":"new"}]}'
        )

        replacements, files = self.local_sdlc.extract_json_artifacts(output, ["app.py"])

        self.assertEqual(files, [])
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].path, "app.py")
        self.assertEqual(replacements[0].replace, "new")

    def test_extracts_fenced_search_replace_artifact(self):
        output = textwrap.dedent(
            """
            Here is the patch:

            ```text
            BEGIN_SEARCH_REPLACE: app.py
            <<<<<<< SEARCH
            old
            =======
            new
            >>>>>>> REPLACE
            END_SEARCH_REPLACE
            ```
            """
        )

        artifacts = self.local_sdlc.extract_search_replace_artifacts(output, ["app.py"])

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].search, "old")
        self.assertEqual(artifacts[0].replace, "new")

    def test_extracts_fenced_file_artifact(self):
        output = textwrap.dedent(
            """
            ```text
            BEGIN_FILE: app.py
            print("ok")
            END_FILE
            ```
            """
        )

        artifacts = self.local_sdlc.extract_file_artifacts(output, ["app.py"])

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].path, "app.py")
        self.assertIn('print("ok")', artifacts[0].content)

    def test_apply_search_replace_rejects_conflict_markers(self):
        artifact = self.local_sdlc.SearchReplaceArtifact(
            path="app.py",
            search="old\n",
            replace="new\n=======\n",
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "app.py").write_text("old\n", encoding="utf-8")

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)

    def test_apply_search_replace_rejects_identical_replacement(self):
        artifact = self.local_sdlc.SearchReplaceArtifact(
            path="app.py",
            search="old\n",
            replace="old\n",
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "app.py").write_text("old\n", encoding="utf-8")

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)

    def test_apply_file_artifact_rejects_conflict_markers(self):
        artifact = self.local_sdlc.FileArtifact(
            path="app.py",
            content="print('x')\n>>>>>>> REPLACE\n",
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.apply_file_artifact(project, artifact, run_dir, 1)

    def test_apply_file_artifact_rejects_nested_artifact_markers(self):
        artifact = self.local_sdlc.FileArtifact(
            path="README.md",
            content="# README\nBEGIN_FILE: PROCESS.md\n# Process\n",
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.apply_file_artifact(project, artifact, run_dir, 1)

    def test_apply_search_replace_rejects_artifact_markers(self):
        artifact = self.local_sdlc.SearchReplaceArtifact(
            path="README.md",
            search="old\n",
            replace="new\nBEGIN_FILE: PROCESS.md\n",
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            run_dir = project / "run"
            run_dir.mkdir()
            (project / "README.md").write_text("old\n", encoding="utf-8")

            with self.assertRaises(self.local_sdlc.RunnerError):
                self.local_sdlc.apply_search_replace_artifact(project, artifact, run_dir, 1)
