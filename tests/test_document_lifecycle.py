"""Document editing behavior through a real, bounded stdio LSP session."""

import json
import os
import queue
import subprocess
import sys
import threading

import pytest


class StdioClient:
    def __init__(self, root, encoding=None):
        self.errors = (root / "server.stderr").open("w+")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-m", "salt_lsp", "--log-level", "warning"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errors,
        )
        self.messages = queue.Queue()
        self.request_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        capabilities = {
            "textDocument": {
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}
            }
        }
        if encoding:
            capabilities["general"] = {"positionEncodings": [encoding]}
        self.capabilities = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root.as_uri(),
                # Lifecycle fixtures are state documents, not an implicit
                # classification of every unconfigured workspace (#6).
                "initializationOptions": {
                    "salt": {
                        "projects": [
                            {
                                "root": root.as_uri(),
                                "profiles": {"default": {"stateRoots": ["."]}},
                            }
                        ]
                    }
                },
                "capabilities": capabilities,
            },
        )["capabilities"]
        self.notify("initialized", {})

    def _read(self):
        try:
            while True:
                headers = {}
                while (line := self.process.stdout.readline()) != b"\r\n":
                    if not line:
                        raise EOFError("server closed stdout")
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                self.messages.put(
                    json.loads(
                        self.process.stdout.read(
                            int(headers["content-length"])
                        )
                    )
                )
        except Exception as error:
            self.messages.put(error)

    def notify(self, method, params=None, request_id=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if request_id is not None:
            message["id"] = request_id
        body = json.dumps(message).encode("utf-8")
        self.process.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        )
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.request_id += 1
        self.notify(method, params, self.request_id)
        while True:
            message = self.messages.get(timeout=10)
            if isinstance(message, Exception):
                raise message
            assert not (
                message.get("method") == "window/showMessage"
                and message["params"]["type"] == 1
            ), message
            if message.get("id") == self.request_id:
                assert "error" not in message, message
                return message["result"]

    def open(self, uri, text, version=1):
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "text": text,
                    "version": version,
                    "languageId": "sls",
                }
            },
        )

    def symbols(self, uri):
        return self.request(
            "textDocument/documentSymbol", {"textDocument": {"uri": uri}}
        )

    def at(self, method, uri, line=0, character=0, **kwargs):
        return self.request(
            "textDocument/" + method,
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                **kwargs,
            },
        )

    def change(self, uri, version, changes):
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": changes,
            },
        )

    def close(self, uri):
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def stop(self):
        try:
            assert self.request("shutdown") is None
            self.notify("exit")
            assert self.process.wait(timeout=10) == 0
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process.stdin.close()
            self.process.stdout.close()
            self.errors.seek(0)
            errors = self.errors.read()
            self.errors.close()
            assert "Traceback" not in errors, errors


@pytest.fixture
def stdio(tmp_path):
    clients = []

    def start(encoding=None):
        client = StdioClient(tmp_path, encoding)
        clients.append(client)
        return client

    yield start
    for client in clients:
        client.stop()


def test_empty_and_outside_nodes_are_safe(stdio, tmp_path):
    client = stdio()
    uri = (tmp_path / "top.sls").as_uri()
    client.open(uri, "")
    assert client.symbols(uri) == []
    assert client.at("definition", uri) is None
    assert client.at("completion", uri) is None
    client.change(uri, 2, [{"text": "# outside recognized nodes\n"}])
    assert client.at("definition", uri, 4, 5) is None
    assert client.at("completion", uri, 4, 5) is None


@pytest.mark.parametrize(
    "encoding,width", [(None, 2), ("utf-8", 4), ("utf-32", 1)]
)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_unicode_symbol_and_definition_ranges(
    stdio, tmp_path, encoding, width, newline
):
    client = stdio(encoding)
    assert client.capabilities["positionEncoding"] == (encoding or "utf-16")
    uri = (tmp_path / "unicode.sls").as_uri()
    client.open(
        uri,
        newline.join(
            [
                "😀: {file.managed: []}",
                "other:",
                "  file.managed:",
                "    - require:",
                "      - file: 😀",
                "",
            ]
        ),
    )
    symbol = client.symbols(uri)[0]
    assert symbol["selectionRange"] == {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": width},
    }
    expected = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 20 + width},
    }
    assert symbol["range"] == expected
    assert client.at("definition", uri, 4, 14 + width) == {
        "uri": uri,
        "range": expected,
    }


@pytest.mark.parametrize(
    "encoding,width", [(None, 2), ("utf-8", 4), ("utf-32", 1)]
)
def test_multiple_edits_incomplete_calls_and_reopen(
    stdio, tmp_path, encoding, width
):
    client = stdio(encoding)
    uri = (tmp_path / "edits.sls").as_uri()
    client.open(uri, "old:\n  file.managed: []\n")
    assert [s["name"] for s in client.symbols(uri)] == ["old"]
    client.change(
        uri,
        2,
        [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 3},
                },
                "text": "new😀",
            },
            {
                "range": {
                    "start": {"line": 0, "character": 3 + width},
                    "end": {"line": 1, "character": 18},
                },
                "text": ":\r\n  file.",
            },
        ],
    )
    symbols = client.symbols(uri)
    assert [s["name"] for s in symbols] == ["new😀"]
    assert symbols[0]["children"][0]["name"] == "file."
    completion = client.at(
        "completion",
        uri,
        1,
        7,
        context={"triggerKind": 2, "triggerCharacter": "."},
    )
    assert completion["isIncomplete"] is False
    managed = next(i for i in completion["items"] if i["label"] == "managed")
    assert managed["data"]["version"] == "3008"
    assert managed["textEdit"] == {
        "range": {
            "start": {"line": 1, "character": 7},
            "end": {"line": 1, "character": 7},
        },
        "newText": "managed",
    }
    client.change(uri, 3, [{"text": "new😀:\n  file..managed: []\n"}])
    assert client.symbols(uri)[0]["children"][0]["name"] == "file..managed"
    client.change(uri, 4, [{"text": ""}])
    assert client.symbols(uri) == []
    client.close(uri)
    assert client.symbols(uri) == []
    assert client.at("definition", uri) is None
    assert (
        client.at(
            "completion",
            uri,
            context={"triggerKind": 2, "triggerCharacter": "."},
        )
        is None
    )
    client.open(uri, "reopened:\n  file.managed: []\n")
    assert [s["name"] for s in client.symbols(uri)] == ["reopened"]


def test_encoded_include_paths_and_removed_includes(stdio, tmp_path):
    directory = tmp_path / "a space #é%"
    directory.mkdir()
    target = directory / "target.sls"
    target.write_text("😀: {file.managed: []}\r\n", encoding="utf-8")
    uri = (directory / "main.sls").as_uri()
    client = stdio()
    # The workspace root contains the directory; use its literal SLS path.
    text = "include:\n  - a space #é%.target\nother:\n  file.managed:\n    - require:\n      - file: 😀\n"
    # Quote the include so # is not a YAML comment.
    text = text.replace("a space #é%.target", "'a space #é%.target'")
    client.open(uri, text)
    location = client.at("definition", uri, 5, 14)
    assert location == {
        "uri": target.as_uri(),
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 22},
        },
    }
    client.change(
        uri,
        2,
        [{"text": text.replace("include:\n  - 'a space #é%.target'\n", "")}],
    )
    assert client.at("definition", uri, 3, 14) is None
    client.close(uri)
    client.open(uri, text.replace("include:\n  - 'a space #é%.target'\n", ""))
    assert client.at("definition", uri, 3, 14) is None


@pytest.mark.parametrize(
    "uri",
    [
        "untitled:buffer",
        "https://example.test/a.sls",
        "file:///tmp/bad%00.sls",
    ],
)
def test_unsupported_document_uris_are_controlled(stdio, tmp_path, uri):
    client = stdio()
    client.open(uri, "ignored:\n  file.managed: []\n")
    assert client.symbols(uri) == []
    assert client.at("definition", uri) is None
    assert client.at("completion", uri) is None
    assert (
        client.at(
            "completion",
            uri,
            context={"triggerKind": 2, "triggerCharacter": "."},
        )
        is None
    )
    client.change(uri, 2, [{"text": ""}])
    client.close(uri)
    valid_uri = (tmp_path / "valid.sls").as_uri()
    client.open(valid_uri, "valid:\n  file.managed: []\n")
    assert client.symbols(valid_uri)[0]["name"] == "valid"


def test_malformed_source_replaces_old_analysis_and_recovers(stdio, tmp_path):
    client = stdio()
    uri = (tmp_path / "malformed.sls").as_uri()
    client.open(uri, "old:\n  file.managed: []\n")
    assert client.symbols(uri)[0]["name"] == "old"
    client.change(uri, 2, [{"text": "\x00"}])
    assert client.symbols(uri) == []
    assert client.at("definition", uri) is None
    client.change(uri, 3, [{"text": "recovered:\n  file.managed: []\n"}])
    assert client.symbols(uri)[0]["name"] == "recovered"


@pytest.mark.parametrize(
    "source",
    [
        "a:\n  file.",
        "a:\n  file: [",
        "a:\n  file: ['",
        "a:\n  file: [}]",
        "\tbad",
        "[]",
        "---\n...",
        "---\na: b\n---\nc: d",
        "a:\n  file.managed:\n    - require:\n      - file:",
    ],
)
def test_incomplete_and_malformed_edits_keep_session_usable(
    stdio, tmp_path, source
):
    client = stdio()
    uri = (tmp_path / "incomplete.sls").as_uri()
    client.open(uri, source)
    assert isinstance(client.symbols(uri), list)
    assert client.at("definition", uri, 10, 0) is None
    client.change(uri, 2, [{"text": "after:\n  file.managed: []\n"}])
    assert client.symbols(uri)[0]["name"] == "after"


@pytest.mark.parametrize(
    "encoding,width", [(None, 2), ("utf-8", 4), ("utf-32", 1)]
)
def test_cursor_and_nested_ranges_after_non_bmp_prefix(
    stdio, tmp_path, encoding, width
):
    client = stdio(encoding)
    uri = (tmp_path / "prefix.sls").as_uri()
    client.open(
        uri,
        "target: {file.managed: []}\n😀other: {file.managed: [{require: [{file: target}]}]}\n",
    )
    assert client.at("definition", uri, 1, 46 + width) == {
        "uri": uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 26},
        },
    }
    call = client.symbols(uri)[1]["children"][0]
    assert call["selectionRange"] == {
        "start": {"line": 1, "character": 8 + width},
        "end": {"line": 1, "character": 20 + width},
    }


def test_superseded_and_closed_edits_do_not_restore_old_state(stdio, tmp_path):
    client = stdio()
    uri = (tmp_path / "versions.sls").as_uri()
    client.open(uri, "first:\n  file.managed: []\n")
    client.change(uri, 3, [{"text": "latest:\n  file.managed: []\n"}])
    client.change(uri, 2, [{"text": "obsolete:\n  file.managed: []\n"}])
    assert client.symbols(uri)[0]["name"] == "latest"
    client.close(uri)
    client.change(uri, 4, [{"text": "closed:\n  file.managed: []\n"}])
    assert client.symbols(uri) == []
    client.close(uri)
    client.open(uri, "reopened:\n  file.managed: []\n", version=1)
    assert client.symbols(uri)[0]["name"] == "reopened"


@pytest.mark.parametrize(
    "encoding,width", [(None, 2), ("utf-8", 4), ("utf-32", 1)]
)
def test_profile_rebuild_preserves_unicode_buffer_and_close(
    stdio, tmp_path, encoding, width
):
    client = stdio(encoding)
    uri = (tmp_path / "switch.sls").as_uri()
    client.open(uri, "old: {file.managed: []}\r\n")
    client.change(uri, 2, [{"text": "😀: {file.managed: []}\r\n"}])

    def switch(active):
        client.notify(
            "workspace/didChangeConfiguration",
            {
                "settings": {
                    "salt": {
                        "projects": [
                            {
                                "root": tmp_path.as_uri(),
                                "activeProfile": active,
                                "profiles": {
                                    "state": {"stateRoots": ["."]},
                                    "pillar": {"pillarRoots": ["."]},
                                },
                            }
                        ]
                    }
                }
            },
        )

    switch("pillar")
    assert client.symbols(uri) == []
    assert client.at("definition", uri) is None
    client.change(uri, 3, [{"text": "new😀: {file.managed: []}\r\n"}])
    switch("state")
    symbol = client.symbols(uri)[0]
    assert symbol["name"] == "new😀"
    assert symbol["selectionRange"]["end"] == {
        "line": 0,
        "character": 3 + width,
    }
    assert symbol["range"]["end"] == {
        "line": 0,
        "character": 23 + width,
    }
    client.change(uri, 2, [{"text": "stale: {file.managed: []}"}])
    assert client.symbols(uri)[0]["name"] == "new😀"
    client.close(uri)
    switch("pillar")
    switch("state")
    assert client.symbols(uri) == []
    client.open(uri, "reopened: {file.managed: []}", version=1)
    assert client.symbols(uri)[0]["name"] == "reopened"
