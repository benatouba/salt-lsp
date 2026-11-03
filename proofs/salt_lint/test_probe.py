"""Executable compatibility evidence; no Salt or server imports required."""

import os
import json
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from probe import probe


class SaltLintCompatibility(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir="/tmp/opencode")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.scratch = tempfile.TemporaryDirectory(dir="/tmp/opencode")
        self.addCleanup(self.scratch.cleanup)
        self.config = self.root / "selected.yaml"
        self.config.write_text("{}\n")
        self.source = self.root / "state.sls"
        self.source.write_text("# saved clean\n")
        self.executable = os.environ["SALT_LINT_EXECUTABLE"]

    def run_probe(self, text, **kwargs):
        def snapshot():
            return {p.relative_to(self.root):
                    (p.is_dir(), None if p.is_dir() else p.read_bytes(),
                     p.stat().st_ino, p.stat().st_mtime_ns)
                    for p in self.root.rglob("*")}

        before = snapshot()
        result = probe(self.executable, self.source, text, self.config,
                       temp_root=self.scratch.name, **kwargs)
        after = snapshot()
        self.assertEqual(before, after, "probe changed the workspace")
        self.assertEqual(list(Path(self.scratch.name).iterdir()), [])
        self.assertFalse(result.faithful)
        return result

    def test_unsaved_content_and_clean_output(self):
        clean = self.run_probe("# clean unsaved\n")
        self.assertEqual(clean.outcome, "clean")
        self.assertEqual(clean.findings, [])
        result = self.run_probe("# unsaved\nvalue: trailing  \n")
        self.assertEqual(result.outcome, "findings")
        finding = next(f for f in result.findings if f["id"] == "201")
        self.assertEqual(finding["linenumber"], 2)
        self.assertEqual(finding["line"], "value: trailing  ")
        self.assertEqual(finding["filename"], str(self.source))
        self.assertNotEqual(result.raw_findings[0]["filename"], str(self.source))
        self.assertFalse(Path(result.raw_findings[0]["filename"]).exists())

    def saved_reference(self):
        """Run the saved-file CLI without its implicit non-TTY stdin file."""
        master, slave = pty.openpty()
        try:
            process = subprocess.run(
                [self.executable, "--json", "--nocolor", "-c", str(self.config),
                 str(self.source)], stdin=slave, cwd=self.config.parent,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                text=True, capture_output=True, timeout=10, check=False,
            )
        finally:
            os.close(master)
            os.close(slave)
        self.assertIn(process.returncode, (0, 2), process.stderr)
        return json.loads(process.stdout) if process.stdout else []

    def test_original_filename_rule_is_lost_before_remapping(self):
        self.source = self.root / "bad.name.sls"
        self.source.write_text("# saved\n")
        self.assertIn("214", {f["id"] for f in self.saved_reference()})
        result = self.run_probe("# different unsaved\n")
        self.assertEqual(result.outcome, "clean")
        self.assertNotIn("214", {f["id"] for f in result.findings})

    def test_jinja_extension_rule_is_lost_before_remapping(self):
        self.source = self.root / "template.jinja"
        self.source.write_text("{{ saved }}\n")
        self.assertIn("205", {f["id"] for f in self.saved_reference()})
        result = self.run_probe("{{ unsaved }}\n")
        self.assertEqual(result.outcome, "clean")
        self.assertNotIn("205", {f["id"] for f in result.findings})

    def test_invalid_selected_configuration_and_cleanup(self):
        self.config.write_text("rules: [broken\n")
        result = self.run_probe("# buffer\n")
        self.assertEqual(result.outcome, "configuration_error")
        self.assertIn("invalid config:", result.stdout)
        # Upstream returns before deleting its stdin file on config failure;
        # the probe must still remove the entire owned temporary directory.
        self.assertEqual(list(Path(self.scratch.name).iterdir()), [])

    def test_missing_selected_configuration_does_not_fall_back(self):
        self.config.unlink()
        result = self.run_probe("# buffer\n")
        self.assertEqual(result.outcome, "configuration_error")

    def install_observing_rule(self):
        rules = self.config.parent / "rules"
        rules.mkdir()
        (self.config.parent / "relative.txt").write_text("config-relative marker")
        (rules / "ObserveRule.py").write_text('''
import json
import os
from pathlib import Path
from saltlint.linter.rule import Rule

class ObserveRule(Rule):
    id = "999"
    shortdesc = "observe the real rule contract"
    description = shortdesc
    severity = "LOW"
    tags = []

    def matchtext(self, file, text):
        message = json.dumps(dict(path=file["path"], type=file["type"],
                                  cwd=os.getcwd(), text=text,
                                  relative=Path("relative.txt").read_text()))
        return [(1, text.splitlines()[0], message)]
''')
        self.config.write_text("rulesdir: [rules]\n")

    def test_config_relative_custom_rules_and_original_path_blocker(self):
        config_dir = self.root / "configuration"
        config_dir.mkdir()
        self.config = config_dir / "selected.yaml"
        self.install_observing_rule()
        result = self.run_probe("# unsaved custom rule input\n")
        self.assertEqual(result.outcome, "findings")
        observation = json.loads(result.findings[0]["message"])
        self.assertEqual(observation["relative"], "config-relative marker")
        self.assertEqual(observation["cwd"], str(config_dir))
        self.assertEqual(observation["text"], "# unsaved custom rule input\n")
        # Output remapping cannot undo what a custom rule already observed.
        self.assertNotEqual(observation["path"], str(self.source))
        self.assertEqual(observation["path"], result.raw_findings[0]["filename"])
        saved = json.loads(self.saved_reference()[0]["message"])
        self.assertEqual(saved["path"], str(self.source))

    def test_all_upstream_classifications_collapse_to_sls_on_stdin(self):
        self.install_observing_rule()
        for suffix, expected in ((".sls", "sls"), (".SLS", "sls"),
                                 (".jinja", "jinja"), (".jinja2", "jinja"),
                                 (".j2", "jinja"), (".yaml", None),
                                 (".txt", None), ("", None)):
            with self.subTest(suffix=suffix):
                self.source = self.root / ("source" + suffix)
                self.source.write_text("# saved\n")
                saved = json.loads(self.saved_reference()[0]["message"])
                self.assertEqual(saved["type"], expected)
                result = self.run_probe("# unsaved\n")
                observed = json.loads(result.findings[0]["message"])
                self.assertEqual(observed["type"], "sls")

    def test_relative_and_absolute_exclusions_do_not_apply_to_stdin(self):
        self.install_observing_rule()
        for excluded in ("state.sls", str(self.source)):
            with self.subTest(excluded=excluded):
                self.config.write_text("rulesdir: [rules]\nexclude_paths: "
                                       + json.dumps([excluded]) + "\n")
                self.assertEqual(self.saved_reference(), [])
                self.assertEqual(self.run_probe("# buffer\n").outcome, "findings")

    def test_per_rule_original_path_ignores_do_not_apply_to_stdin(self):
        self.install_observing_rule()
        self.config.write_text('rulesdir: [rules]\nrules:\n'
                               '  "999":\n    ignore: "**/state.sls"\n')
        self.assertEqual(self.saved_reference(), [])
        self.assertEqual(self.run_probe("# buffer\n").outcome, "findings")

    def test_explicit_config_still_imports_cwd_default_config_upstream(self):
        (self.root / ".salt-lint").write_text("rules: [invalid\n")
        result = self.run_probe("# buffer\n")
        self.assertEqual(result.outcome, "execution_error")
        self.assertIn("SaltLintConfigError", result.stderr)

    def test_ancestor_and_home_configuration_are_not_consumed(self):
        (self.root / ".salt-lint").write_text("rules: [invalid ancestor\n")
        home = self.root / "home"
        home.mkdir()
        (home / ".salt-lint").write_text("rules: [invalid global\n")
        project = self.root / "project"
        project.mkdir()
        self.config = project / "selected.yaml"
        self.config.write_text("{}\n")
        with patch.dict(os.environ, HOME=str(home)):
            result = self.run_probe("value: trailing  \n")
        self.assertEqual(result.outcome, "findings")
        self.assertIn("201", {f["id"] for f in result.findings})

    def test_real_custom_rule_stdout_is_malformed_not_clean(self):
        self.install_observing_rule()
        rule = self.config.parent / "rules" / "ObserveRule.py"
        rule.write_text(rule.read_text().replace(
            'message = json.dumps', 'print("custom rule stdout")\n        message = json.dumps'))
        result = self.run_probe("# buffer\n")
        self.assertEqual(result.outcome, "malformed_output")
        self.assertIn("custom rule stdout", result.stdout)
        self.assertEqual(result.findings, [])

    def test_selected_config_can_disable_json_despite_json_cli_flag(self):
        self.config.write_text("json: false\n")
        result = self.run_probe("value: trailing  \n")
        self.assertEqual(result.outcome, "malformed_output")
        self.assertEqual(result.findings, [])

    def test_custom_rule_import_failure_is_execution_failure_and_cleans_up(self):
        self.install_observing_rule()
        (self.config.parent / "rules" / "ObserveRule.py").write_text(
            'raise RuntimeError("deliberate custom rule import failure")\n')
        result = self.run_probe("# buffer\n")
        self.assertEqual(result.outcome, "execution_error")
        self.assertIn("deliberate custom rule import failure", result.stderr)

    def controlled_tool(self, body, version="salt-lint 0.9.2"):
        script = self.root / "controlled-tool"
        script.write_text(f"#!{sys.executable}\nimport sys\n"
                          f"if '--version' in sys.argv:\n    print({version!r})\n"
                          f"    sys.exit(0)\n{body}\n")
        script.chmod(0o700)
        self.executable = str(script)

    def test_unknown_tool_version_is_not_claimed_supported(self):
        self.controlled_tool("sys.exit(0)", version="salt-lint 99.0")
        self.assertEqual(self.run_probe("# buffer\n").outcome, "unsupported_version")

    def test_missing_executable_is_not_clean(self):
        self.executable = str(self.root / "not-installed")
        self.assertEqual(self.run_probe("# buffer\n").outcome, "execution_error")

    def test_timeout_is_execution_failure_and_cleans_up(self):
        self.controlled_tool("while True: pass")
        self.assertEqual(self.run_probe("# buffer\n", timeout=0.2).outcome,
                         "execution_error")

    def test_malformed_json_schema_and_foreign_paths_are_rejected(self):
        samples = ["null", "{}", "[1]", '[{"id": "201"}]',
                   '[{"id":"201","message":"m","filename":"/other/file.sls",'
                   '"line":"x","linenumber":1,"severity":"LOW"}]']
        for output in samples:
            with self.subTest(output=output):
                self.controlled_tool(f"print({output!r})\nsys.exit(2)")
                result = self.run_probe("# buffer\n")
                self.assertEqual(result.outcome, "malformed_output")
                self.assertEqual(result.findings, [])


if __name__ == "__main__":
    unittest.main()
