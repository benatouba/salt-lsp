"""Dependency-free stdio acceptance probe; run with the installed Python.

Usage: /path/to/venv/bin/python tests/installed_smoke.py [server command ...]
The default command is this interpreter's ``-m salt_lsp``.
"""

import importlib.util
from importlib.resources import files
import gzip
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading


def smoke(command):
    assert (
        importlib.util.find_spec("salt") is None
    ), "Salt must not be installed"
    package = importlib.util.find_spec("salt_lsp")
    assert package is not None and package.origin is not None
    package_path = Path(package.origin).resolve().parent
    assert package_path.is_relative_to(
        Path(sys.prefix).resolve()
    ), f"expected an installed artifact, not a source checkout: {package_path}"
    assert not (package_path / "data" / "states.pickle").exists()
    schema = json.loads(
        files("salt_lsp").joinpath("project.schema.json").read_text()
    )
    assert (
        schema["$defs"]["profile"]["properties"]["saltVersion"]["default"]
        == "3008"
    )
    for version, release, source_count in (
        ("3006", "3006.30", 357),
        ("3007", "3007.17", 355),
        ("3008", "3008.5", 130),
    ):
        catalog = json.loads(
            gzip.decompress(
                files("salt_lsp")
                .joinpath(f"catalogs/states-{version}.json.gz")
                .read_bytes()
            )
        )
        assert catalog["schema"] == 1
        assert catalog["provenance"]["release"] == release
        assert len(catalog["coverage"]) == source_count
        assert len(catalog["provenance"]["sha256"]) == 64
        assert len(catalog["generator"]["sha256"]) == 64
        assert any(
            e["name"] == "file.managed"
            and e["arguments"]
            and e["documentation"]
            for e in catalog["entries"]
        )
        assert any(
            e["name"] == "cmd.watch" and e["alias_of"] == "wait"
            for e in catalog["entries"]
        )
        assert any(
            e["name"] == "pkg.refresh_db"
            and e["definition"]["path"] == "salt/modules/win_pkg.py"
            for e in catalog["entries"]
        )
        assert (
            any(
                e["name"] == "dsc_resource.managed" for e in catalog["entries"]
            )
        ) == (version == "3008")
        assert (
            any(
                e["name"] == "docker_container.running"
                for e in catalog["entries"]
            )
        ) == (version != "3008")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="salt-lsp-smoke-") as directory:
        root = Path(directory)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            messages = queue.Queue()

            def read_messages():
                try:
                    while True:
                        headers = {}
                        while (line := process.stdout.readline()) != b"\r\n":
                            if not line:
                                raise EOFError("server closed stdout")
                            key, value = line.decode("ascii").split(":", 1)
                            headers[key.lower()] = value.strip()
                        body = process.stdout.read(
                            int(headers["content-length"])
                        )
                        messages.put(json.loads(body))
                except Exception as error:
                    messages.put(error)

            threading.Thread(target=read_messages, daemon=True).start()

            def send(method, params=None, request_id=None):
                message = {"jsonrpc": "2.0", "method": method}
                if params is not None:
                    message["params"] = params
                if request_id is not None:
                    message["id"] = request_id
                body = json.dumps(message).encode("utf-8")
                process.stdin.write(
                    f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
                process.stdin.flush()

            def response(request_id):
                while True:
                    message = messages.get(timeout=15)
                    if isinstance(message, Exception):
                        raise message
                    if message.get("id") == request_id:
                        assert "error" not in message, message
                        return message["result"]

            try:
                send(
                    "initialize",
                    {
                        "processId": os.getpid(),
                        "rootUri": root.as_uri(),
                        "initializationOptions": {
                            "salt": {
                                "projects": [
                                    {
                                        "root": root.as_uri(),
                                        "profiles": {
                                            "default": {"stateRoots": ["."]}
                                        },
                                    }
                                ]
                            }
                        },
                        "capabilities": {},
                    },
                    1,
                )
                capabilities = response(1)["capabilities"]
                assert capabilities["documentSymbolProvider"]
                assert capabilities["hoverProvider"]
                send("initialized", {})
                uri = (root / "example.sls").as_uri()
                send(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "sls",
                            "version": 1,
                            "text": "example:\n  file.managed:\n    - name: /tmp/example\n",
                        }
                    },
                )
                send(
                    "textDocument/documentSymbol",
                    {
                        "textDocument": {"uri": uri},
                    },
                    2,
                )
                symbols = response(2)
                assert len(symbols) == 1, symbols
                assert symbols[0]["name"] == "example", symbols
                assert symbols[0]["range"]["start"]["line"] == 0, symbols
                # Real completions use packaged catalogs without Salt installed.
                send(
                    "textDocument/completion",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": 1, "character": 7},
                        "context": {"triggerKind": 2, "triggerCharacter": "."},
                    },
                    3,
                )
                completion = response(3)
                managed = next(
                    i for i in completion["items"] if i["label"] == "managed"
                )
                assert "3008.5" in managed["detail"]
                assert "salt/states/file.py" in managed["documentation"]
                assert managed["textEdit"]["range"]["start"] == {
                    "line": 1,
                    "character": 7,
                }
                assert managed["textEdit"]["range"]["end"] == {
                    "line": 1,
                    "character": 14,
                }
                assert managed["textEdit"]["newText"] == "managed"
                send(
                    "textDocument/hover",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": 1, "character": 7},
                    },
                    8,
                )
                documented = response(8)["contents"]["value"]
                assert documented.startswith("file.managed("), documented
                assert "Salt 3008.5" in documented, documented
                send(
                    "textDocument/completion",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": 2, "character": 6},
                    },
                    9,
                )
                arguments = {i["label"] for i in response(9)["items"]}
                # The entry under the cursor is the one being edited: its own
                # name stays available alongside the function's other arguments.
                assert {"name", "source"} <= arguments, arguments
                send("salt/projectContext", {"uri": uri}, 4)
                context = response(4)
                assert context["saltVersion"] == "3008"
                assert context["versionSource"] == "default"
                assert context["projectRoot"] == root.as_uri()
                assert context["stateRoots"] == [root.as_uri()]
                assert context["fileRole"] == "state"
                send(
                    "workspace/didChangeConfiguration",
                    {
                        "settings": {
                            "salt": {
                                "projects": [
                                    {
                                        "root": root.as_uri(),
                                        "activeProfile": "next",
                                        "profiles": {
                                            "next": {
                                                "saltVersion": "3007",
                                                "environment": "dev",
                                                "stateRoots": ["."],
                                            }
                                        },
                                    }
                                ]
                            }
                        }
                    },
                )
                send("salt/projectContext", {"uri": uri}, 5)
                updated = response(5)
                assert updated["saltVersion"] == "3007"
                assert updated["versionSource"] == "configured"
                assert updated["activeProfile"] == "next"
                assert updated["environment"] == "dev"
                assert updated["revision"] > context["revision"]
                send(
                    "textDocument/completion",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": 1, "character": 7},
                    },
                    7,
                )
                switched = response(7)
                assert switched["items"]
                assert all(
                    i["data"]["version"] == "3007" for i in switched["items"]
                )
                send("shutdown", request_id=6)
                assert response(6) is None
                send("exit")
                assert process.wait(timeout=15) == 0
                assert not list(
                    root.iterdir()
                ), "server wrote into the workspace"
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                errors.seek(0)
                print(
                    errors.read().decode("utf-8", errors="replace"),
                    file=sys.stderr,
                )
                raise
            finally:
                process.stdin.close()
                process.stdout.close()
    print(
        f"PASS installed stdio initialize/open/symbols/completion/profiles/shutdown: {command}"
    )


if __name__ == "__main__":
    smoke(sys.argv[1:] or [sys.executable, "-I", "-m", "salt_lsp"])
