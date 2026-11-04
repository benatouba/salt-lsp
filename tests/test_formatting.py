"""Standard formatting requests never rewrite sources or format templates."""

import os
import pytest
from test_projects import clients  # pylint: disable=unused-import
from test_tools import executable


def format_document(client, path):
    return client.request(
        "textDocument/formatting",
        {
            "textDocument": {"uri": path.as_uri()},
            "options": {"tabSize": 2, "insertSpaces": True},
        },
    )


def test_standard_formatting_trust_unsaved_input_and_template_refusal(
    tmp_path, clients
):
    marker = tmp_path / "input"
    binary = executable(
        tmp_path / "yamlfmt",
        f"""import sys
from pathlib import Path
if '-version' in sys.argv:
    print('yamlfmt 0.21.0 (fixture)')
else:
    text = sys.stdin.read()
    Path({str(marker)!r}).write_text(text)
    print(text.replace('    ', '  '), end='')
""",
    )
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": str(tmp_path),
                    "profiles": {"default": {"stateRoots": ["."]}},
                }
            ],
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ],
        },
    )
    path = tmp_path / "a.sls"
    path.write_text("saved: disk\n")
    source = "é😀:\n    nested: value\n"
    client.open(path, source)
    assert format_document(client, path) == []
    assert not marker.exists()
    params = {"uri": path.as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    assert format_document(client, path) == [
        {
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 2, "character": 0},
            },
            "newText": "é😀:\n  nested: value\n",
        }
    ]
    assert marker.read_text() == source
    assert path.read_text() == "saved: disk\n"
    for version, text in enumerate(
        (
            "id:\n  value: {{ grains.id }}\n",
            "#!py\nvalue: thing\n",
            "id:\n  value: !custom thing\n",
        ),
        2,
    ):
        client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": path.as_uri(), "version": version},
                "contentChanges": [{"text": text}],
            },
        )
        assert format_document(client, path) == []
    assert marker.read_text() == source
    messages = [
        message
        for message in client.notifications
        if message.get("method") == "window/showMessage"
    ]
    assert messages


def test_real_yamlfmt_standard_request(tmp_path, clients):
    binary = os.environ.get("SALT_LSP_TEST_YAMLFMT")
    if not binary:
        pytest.skip("Set SALT_LSP_TEST_YAMLFMT to yamlfmt 0.21.0")
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": str(tmp_path),
                    "profiles": {"default": {"stateRoots": ["."]}},
                }
            ],
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ],
        },
    )
    path = tmp_path / "a.sls"
    path.write_text("saved: disk\n")
    client.open(path, "key:\n    nested: value\n")
    params = {"uri": path.as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    assert (
        format_document(client, path)[0]["newText"]
        == "key:\n  nested: value\n"
    )
    assert path.read_text() == "saved: disk\n"


def test_stale_formatter_result_is_discarded(tmp_path, clients):
    binary = executable(
        tmp_path / "yamlfmt",
        """import sys, time
if '-version' in sys.argv:
    print('yamlfmt 0.21.0 (fixture)')
else:
    time.sleep(.4)
    print('obsolete: output')
""",
    )
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": str(tmp_path),
                    "profiles": {"default": {"stateRoots": ["."]}},
                }
            ],
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ],
        },
    )
    path = tmp_path / "a.sls"
    client.open(path, "old: buffer\n")
    params = {"uri": path.as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    client.sequence += 1
    sequence = client.sequence
    client.send(
        {
            "id": sequence,
            "method": "textDocument/formatting",
            "params": {
                "textDocument": {"uri": path.as_uri()},
                "options": {"tabSize": 2, "insertSpaces": True},
            },
        }
    )
    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": 2},
            "contentChanges": [{"text": "new: buffer\n"}],
        },
    )
    while True:
        message = client.messages.get(timeout=10)
        if message.get("id") == sequence:
            assert message["result"] == []
            break
        client.notifications.append(message)
