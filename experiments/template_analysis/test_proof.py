"""Contract tests at the standalone source-in / JSON-out experiment seam."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).parent


def run(source):
    result = subprocess.run(
        [sys.executable, "-I", str(HERE / "proof.py")],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


class ProofTests(unittest.TestCase):
    def test_nested_controls_aliases_and_yaml_tags_remain_nonexecuting(self):
        source = (HERE / "fixtures/nested.sls").read_text()
        result = run(source)
        declarations = result["declarations"]
        self.assertEqual(
            [(d["value"], d["certainty"]) for d in declarations],
            [
                ("nested", "conditional"),
                ("file.managed", "conditional"),
                ("outside", "literal"),
                ("test.nop", "literal"),
            ],
        )
        self.assertIn(
            "malformed_yaml", [u["kind"] for u in result["unknowns"]]
        )

    def test_delimiter_edit_prefixes_never_crash(self):
        source = (HERE / "fixtures/mixed.sls").read_text()
        points = {0, len(source)}
        for match in re.finditer(r"{{|}}|{%|%}|[\[\]'\"]", source):
            points.update({match.start(), match.start() + 1, match.end()})
        for end in sorted(points):
            with self.subTest(end=end):
                result = run(source[:end])
                for item in result["declarations"] + result["references"]:
                    self.assertLessEqual(item["end"], end)

    def test_unknown_references_reserved_keys_and_non_yaml_renderer(self):
        result = run(
            "{% include base ~ '.jinja' %}\ninclude:\n"
            "  - {{ target }}\nextend:\n  other:\n"
            "    file.managed: []\nexclude:\n  - sls: old\n"
            "ok:\n  test.nop: []\n"
        )
        self.assertEqual(
            [d["value"] for d in result["declarations"]], ["ok", "test.nop"]
        )
        self.assertEqual(result["references"], [])
        self.assertTrue(
            {"dynamic_reference", "generated_reference", "reserved_key"}
            <= {u["kind"] for u in result["unknowns"]}
        )
        other = run("#!py\nstate:\n  test.nop: []\n")
        self.assertEqual(other["declarations"], [])
        self.assertEqual(other["unknowns"][0]["kind"], "unsupported_renderer")

    def test_escaped_template_reference_is_not_invented(self):
        result = run("{% include 'a\\nb.jinja' %}\nok:\n  test.nop: []\n")
        self.assertEqual(result["references"], [])
        self.assertIn(
            "dynamic_reference", [u["kind"] for u in result["unknowns"]]
        )

    def test_quoted_delimiters_raw_blocks_and_crlf_ranges(self):
        source = (
            "{% raw %}\r\nraw:\r\n  test.nop:\r\n"
            "    - name: '{{ not_jinja }}'\r\n{% endraw %}\r\n"
            "quoted:\r\n  file.managed:\r\n"
            "    - name: {{ {'end': '}}'} }}\r\n"
            "last:\r\n  test.nop: []\r\n"
        )
        result = run(source)
        expressions = [
            u for u in result["unknowns"] if u["kind"] == "expression"
        ]
        self.assertEqual(len(expressions), 1)
        self.assertEqual(
            source[expressions[0]["start"] : expressions[0]["end"]],
            "{{ {'end': '}}'} }}",
        )
        last = next(d for d in result["declarations"] if d["value"] == "last")
        self.assertEqual(
            (last["start"], last["line"], last["column"]),
            (source.index("last:"), 8, 0),
        )

    def test_sentinels_cannot_execute_during_analysis(self):
        with tempfile.TemporaryDirectory(
            dir="/tmp/opencode" if Path("/tmp/opencode").exists() else None
        ) as directory:
            env = dict(os.environ, PROOF_SENTINEL=directory + "/executed")
            env["PATH"] = (
                str(HERE / "fixtures/sentinel") + os.pathsep + env["PATH"]
            )
            command = [sys.executable, "-I", str(HERE / "guard.py")]
            result = subprocess.run(
                command,
                input=(HERE / "fixtures/sentinel.sls").read_text(),
                text=True,
                capture_output=True,
                env=env,
                check=True,
                cwd=HERE / "fixtures/sentinel",
            )
            self.assertIn(
                "safe",
                [
                    d["value"]
                    for d in json.loads(result.stdout)["declarations"]
                ],
            )
            self.assertFalse(Path(env["PROOF_SENTINEL"]).exists())
            # Positive controls: both fixtures really do write if executed.
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(HERE / "fixtures/sentinel/workspace_probe.py"),
                ],
                env=env,
                check=True,
            )
            self.assertTrue(Path(env["PROOF_SENTINEL"]).exists())
            Path(env["PROOF_SENTINEL"]).unlink()
            subprocess.run(
                ["salt", "--version"],
                env=env,
                check=True,
            )
            self.assertTrue(Path(env["PROOF_SENTINEL"]).exists())

    def test_broken_islands_and_incomplete_edits_keep_later_assistance(self):
        source = (HERE / "fixtures/incomplete.sls").read_text()
        result = run(source)
        values = [d["value"] for d in result["declarations"]]
        self.assertEqual(
            values,
            [
                "before",
                "test.nop",
                "broken",
                "file.managed",
                "unfinished",
                "test.nop",
                "after",
                "service.running",
            ],
        )
        kinds = {u["kind"] for u in result["unknowns"]}
        self.assertTrue({"malformed_yaml", "incomplete_template"} <= kinds)
        self.assertFalse(result["comparisons"]["jinja_syntax"]["accepted"])
        after = next(
            d for d in result["declarations"] if d["value"] == "after"
        )
        self.assertEqual(after["start"], source.index("after:"))

    def test_mixed_templates_preserve_literals_but_not_generated_values(self):
        source = (HERE / "fixtures/mixed.sls").read_text()
        result = run(source)
        self.assertEqual(
            [
                (d["kind"], d["value"], d["certainty"])
                for d in result["declarations"]
            ],
            [
                ("state", "conditional", "conditional"),
                ("function", "pkg.installed", "conditional"),
                ("function", "service.running", "conditional"),
                ("state", "after", "literal"),
                ("function", "test.nop", "literal"),
            ],
        )
        self.assertEqual(
            [(r["kind"], r["value"]) for r in result["references"]],
            [
                ("import", "map.jinja"),
                ("from", "lib.jinja"),
                ("include", "fragment.jinja"),
                ("sls_include", "common"),
            ],
        )
        self.assertIn("generated_key", [u["kind"] for u in result["unknowns"]])
        for item in result["declarations"] + result["references"]:
            text = source[item["start"] : item["end"]]
            self.assertEqual(text.strip("'\""), item["value"])
        self.assertFalse(result["comparisons"]["raw_yaml"]["accepted"])
        self.assertTrue(result["comparisons"]["jinja_syntax"]["accepted"])

    def test_plain_state_has_exact_source_ranges(self):
        source = (HERE / "fixtures/plain.sls").read_text()
        result = run(source)
        self.assertEqual(
            result["declarations"],
            [
                {
                    "kind": "state",
                    "value": "café-😀",
                    "start": 0,
                    "end": 6,
                    "certainty": "literal",
                    "line": 0,
                    "column": 0,
                },
                {
                    "kind": "function",
                    "value": "file.managed",
                    "start": 10,
                    "end": 22,
                    "certainty": "literal",
                    "line": 1,
                    "column": 2,
                },
            ],
        )
        self.assertEqual(result["unknowns"], [])
        self.assertTrue(result["comparisons"]["raw_yaml"]["accepted"])


if __name__ == "__main__":
    unittest.main()
