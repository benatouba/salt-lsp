"""Lint scheduling and ownership through real subprocess LSP sessions."""

import json
import time
import os

import pytest

from test_projects import clients  # pylint: disable=unused-import
from test_tools import executable


CAPABILITIES = {
    "protocol": 1,
    "salt_lint": "0.9.2",
    "python": [3, 12],
    "pyyaml": "6.0.3",
    "pathspec": "1.1.1",
    "stdin_filename": True,
}


def settings(root, binary):
    return {
        "projects": [
            {"root": str(root), "profiles": {"default": {"stateRoots": ["."]}}}
        ],
        "tools": [
            {
                "projectRoot": str(root),
                "name": "salt-lint",
                "executable": binary,
            }
        ],
    }


def approve(client, path):
    params = {"uri": path.as_uri(), "tool": "salt-lint"}
    subject = client.request("salt/toolStatus", params)
    return client.request(
        "salt/authorizeTool",
        {**params, "token": subject["token"], "action": "allow"},
    )


def lint_findings(client, path):
    client.symbols(path)
    messages = [
        message["params"]["diagnostics"]
        for message in client.notifications
        if message.get("method") == "textDocument/publishDiagnostics"
        and message["params"]["uri"] == path.as_uri()
    ]
    return (
        [item for item in messages[-1] if item["source"] == "salt-lint"]
        if messages
        else []
    )


def wait_lint(client, path):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        status = client.request("salt/lintStatus", {"uri": path.as_uri()})
        if status["status"] != "pending":
            return status
        time.sleep(0.05)
    raise AssertionError("Lint did not finish")


def test_lint_trust_trailing_debounce_unsaved_findings_and_clear(
    tmp_path, clients
):
    marker = tmp_path / "runs"
    binary = executable(
        tmp_path / "salt-lint",
        f"""import sys, json
from pathlib import Path
if '--salt-lsp-capabilities' in sys.argv:
    print({json.dumps(json.dumps(CAPABILITIES))})
else:
    text = sys.stdin.read()
    with Path({str(marker)!r}).open('a') as log:
        log.write(text.replace('\\n', '|') + '\\n')
    findings = [{{'id': '901', 'message': 'Known finding', 'filename': sys.argv[sys.argv.index('--stdin-filename') + 1], 'linenumber': 1, 'severity': 'HIGH'}}] if 'bad' in text else []
    print(json.dumps(findings))
    sys.exit(2 if findings else 0)
""",
    )
    client = clients(tmp_path, settings=settings(tmp_path, binary))
    path = tmp_path / "a.sls"
    path.write_text("saved: disk\n")
    client.open(path, "bad: buffer\n")
    time.sleep(1.1)
    assert not marker.exists()
    approve(client, path)
    time.sleep(0.4)
    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": 2},
            "contentChanges": [{"text": "bad: newest\n"}],
        },
    )
    time.sleep(0.7)
    assert not marker.exists()
    time.sleep(0.6)
    findings = lint_findings(client, path)
    assert len(findings) == 1
    assert findings[0]["code"] == "901"
    assert findings[0]["severity"] == 2
    assert findings[0]["range"]["start"] == {"line": 0, "character": 0}
    assert marker.read_text() == "bad: newest|\n"
    assert path.read_text() == "saved: disk\n"
    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": 3},
            "contentChanges": [{"text": "good: buffer\n"}],
        },
    )
    assert lint_findings(client, path) == []
    time.sleep(1.3)
    assert (
        client.request("salt/lintStatus", {"uri": path.as_uri()})["status"]
        == "clean"
    )
    assert lint_findings(client, path) == []


def test_real_patched_lint_original_path_custom_rules_and_exclusions(
    tmp_path, clients
):
    binary = os.environ.get("SALT_LSP_TEST_SALT_LINT")
    if not binary:
        pytest.skip(
            "Set SALT_LSP_TEST_SALT_LINT to the documented patched adapter"
        )
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "ObserveRule.py").write_text(
        """from pathlib import Path
from saltlint.linter.rule import Rule
class ObserveRule(Rule):
    id = '999'
    shortdesc = description = 'Observe callback inputs'
    severity = 'LOW'
    tags = ['observe']
    def matchtext(self, file, text):
        assert Path('relative.txt').read_text() == 'resource'
        assert Path(file['path']).read_text() == 'saved disk\\n'
        return [(1, '', 'unsaved callback: ' + text)]
"""
    )
    (tmp_path / "relative.txt").write_text("resource")
    (tmp_path / ".salt-lint").write_text(
        "rules: [invalid implicit configuration\n"
    )
    config = tmp_path / "selected.yaml"
    config.write_text("rulesdir: [rules]\njson: false\nverbosity: 2\n")
    configuration = settings(tmp_path, binary)
    configuration["tools"][0].update(
        config="selected.yaml", ruleDirs=["rules"]
    )
    client = clients(tmp_path, settings=configuration)
    path = tmp_path / "state.sls"
    path.write_text("saved disk\n")
    client.open(path, "unsaved buffer\n")
    approve(client, path)
    wait_lint(client, path)
    found = lint_findings(client, path)
    assert [(item["code"], item["message"]) for item in found] == [
        ("999", "unsaved callback: unsaved buffer\n")
    ], client.request("salt/lintStatus", {"uri": path.as_uri()})
    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": 2},
            "contentChanges": [{"text": ""}],
        },
    )
    wait_lint(client, path)
    assert lint_findings(client, path)[0]["message"] == "unsaved callback: "
    config.write_text("rulesdir: [rules]\nexclude_paths: [state.sls]\n")
    assert (
        client.request(
            "salt/toolStatus", {"uri": path.as_uri(), "tool": "salt-lint"}
        )["status"]
        == "denied-trust"
    )
    approve(client, path)
    wait_lint(client, path)
    assert lint_findings(client, path) == []
    assert (
        client.request("salt/lintStatus", {"uri": path.as_uri()})["status"]
        == "clean"
    )
    assert path.read_text() == "saved disk\n"


@pytest.mark.parametrize(
    "output,code",
    [
        ("broken configuration", 2),
        ('[{"message":"incomplete"}]', 2),
        ("[]", 2),
    ],
)
def test_failed_lint_is_status_not_clean_or_findings(
    tmp_path, clients, output, code
):
    binary = executable(
        tmp_path / "salt-lint",
        f"""import sys
if '--salt-lsp-capabilities' in sys.argv:
    print({json.dumps(json.dumps(CAPABILITIES))})
else:
    print({output!r})
    sys.exit({code})
""",
    )
    client = clients(tmp_path, settings=settings(tmp_path, binary))
    path = tmp_path / "a.sls"
    client.open(path, "include:\n  - missing\n")
    approve(client, path)
    assert wait_lint(client, path)["status"] == "invalid-result"
    assert lint_findings(client, path) == []
    assert any(
        item["source"] == "salt-lsp"
        for message in client.notifications
        if message.get("method") == "textDocument/publishDiagnostics"
        for item in message["params"]["diagnostics"]
    )


def test_obsolete_running_lint_cancelled_and_revocation_clears(
    tmp_path, clients
):
    started = tmp_path / "started"
    binary = executable(
        tmp_path / "salt-lint",
        f"""import sys, time, json
from pathlib import Path
if '--salt-lsp-capabilities' in sys.argv:
    print({json.dumps(json.dumps(CAPABILITIES))})
else:
    text = sys.stdin.read()
    if 'slow' in text:
        Path({str(started)!r}).touch()
        time.sleep(5)
    result = [{{'id':'999','message':text,'filename':sys.argv[-1],'linenumber':1,'severity':'LOW'}}] if 'bad' in text else []
    print(json.dumps(result))
    sys.exit(2 if result else 0)
""",
    )
    client = clients(tmp_path, settings=settings(tmp_path, binary))
    path = tmp_path / "a.sls"
    client.open(path, "slow bad: old\n")
    approve(client, path)
    deadline = time.monotonic() + 7
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()
    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": 2},
            "contentChanges": [{"text": "bad: latest\n"}],
        },
    )
    assert wait_lint(client, path)["status"] == "findings"
    assert [item["message"] for item in lint_findings(client, path)] == [
        "bad: latest\n"
    ]
    client.request(
        "salt/authorizeTool",
        {
            "uri": path.as_uri(),
            "tool": "salt-lint",
            "token": "",
            "action": "revoke",
        },
    )
    assert lint_findings(client, path) == []
    assert (
        client.request("salt/lintStatus", {"uri": path.as_uri()})["status"]
        == "denied-trust"
    )
