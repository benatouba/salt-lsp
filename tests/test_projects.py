"""Project acceptance at the actual subprocess stdio boundary."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest


class StdioClient:
    def __init__(
        self,
        root,
        folders=None,
        settings=None,
        stderr=subprocess.DEVNULL,
        capabilities=None,
    ):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "salt_lsp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        self.messages = queue.Queue()
        self.notifications = []
        self.sequence = 0

        def read():
            try:
                while True:
                    headers = {}
                    while (line := self.process.stdout.readline()) != b"\r\n":
                        if not line:
                            raise EOFError("server closed stdout")
                        key, value = line.decode().split(":", 1)
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

        threading.Thread(target=read, daemon=True).start()
        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root.as_uri(),
                "workspaceFolders": [
                    {"uri": p.as_uri(), "name": p.name}
                    for p in (folders or [root])
                ],
                "capabilities": (
                    capabilities
                    if capabilities is not None
                    else {
                        "textDocument": {
                            "documentSymbol": {
                                "hierarchicalDocumentSymbolSupport": True
                            }
                        }
                    }
                ),
                "initializationOptions": {"salt": settings or {}},
            },
        )
        self.notify("initialized", {})

    def send(self, message):
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        self.process.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        self.process.stdin.flush()

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def request(self, method, params=None):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        while True:
            message = self.messages.get(timeout=10)
            if isinstance(message, Exception):
                raise message
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message.get("result")
            self.notifications.append(message)

    def context(self, path):
        return self.request("salt/projectContext", {"uri": path.as_uri()})

    def open(
        self,
        path,
        text="example:\n  file.managed:\n    - name: /tmp/example\n",
    ):
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": "sls",
                    "version": 1,
                    "text": text,
                }
            },
        )

    def symbols(self, path):
        return self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path.as_uri()}},
        )

    def close(self):
        if self.process.poll() is None:
            try:
                self.request("shutdown")
                self.notify("exit", {})
                assert self.process.wait(timeout=10) == 0
            finally:
                if self.process.poll() is None:
                    self.process.kill()
        self.process.stdin.close()
        self.process.stdout.close()


@pytest.fixture
def clients():
    running = []

    def start(*args, **kwargs):
        client = StdioClient(*args, **kwargs)
        running.append(client)
        return client

    yield start
    for client in running:
        client.close()


def test_nearest_git_worktree_and_visible_default_with_workspace_fallback(
    tmp_path, clients
):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: /not/read/or/executed\n")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    client = clients(tmp_path, folders=[tmp_path, outside])
    context = client.context(nested / "states" / "a.sls")
    assert context["projectRoot"] == nested.as_uri()
    assert context["saltVersion"] == "3008"
    assert context["versionSource"] == "default"
    assert context["activeProfile"] == "default"
    assert context["environment"] == "base"
    assert context["fileRole"] == "generic"
    assert (
        client.context(tmp_path / "a.sls")["projectRoot"] == tmp_path.as_uri()
    )
    assert client.context(outside / "a.sls")["projectRoot"] == outside.as_uri()
    assert any(
        "3008" in n.get("params", {}).get("message", "")
        for n in client.notifications
    )


def test_explicit_root_profiles_and_separate_content_roots(tmp_path, clients):
    (tmp_path / ".git").mkdir()
    settings = {
        "projects": [
            {
                "workspaceUri": tmp_path.as_uri(),
                "root": "deployment",
                "activeProfile": "production",
                "profiles": {
                    "production": {
                        "saltVersion": "3006",
                        "environment": "prod",
                        "stateRoots": ["states/prod"],
                        "pillarRoots": ["pillar/prod"],
                        "moduleDirs": ["extensions"],
                    },
                    "next": {
                        "saltVersion": "3008",
                        "stateRoots": ["states/next"],
                    },
                },
            }
        ]
    }
    client = clients(tmp_path, settings=settings)
    context = client.context(tmp_path / "deployment" / "states/prod/a.sls")
    assert context["projectRoot"] == (tmp_path / "deployment").as_uri()
    assert context["activeProfile"] == "production"
    assert context["saltVersion"] == "3006"
    assert context["versionSource"] == "configured"
    assert context["environment"] == "prod"
    assert context["stateRoots"] == [
        (tmp_path / "deployment/states/prod").as_uri()
    ]
    assert context["pillarRoots"] == [
        (tmp_path / "deployment/pillar/prod").as_uri()
    ]
    assert context["moduleDirs"] == [
        (tmp_path / "deployment/extensions").as_uri()
    ]


def test_roles_are_conservative_and_configuration_never_executes(
    tmp_path, clients, monkeypatch
):
    sentinel = tmp_path / "EXECUTED"
    commands = tmp_path / "bin"
    commands.mkdir()
    git = commands / "git"
    git.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    (tmp_path / "extensions").mkdir()
    (tmp_path / "extensions/evil.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    )
    # Neither project configuration nor Salt/Jinja/Python files are executed.
    (tmp_path / "Saltfile").write_text(
        f"!!python/object/apply:os.system ['touch {sentinel}']"
    )
    profile = {
        "stateRoots": ["states", "shared"],
        "pillarRoots": ["pillar", "shared"],
        "moduleDirs": ["extensions"],
        "fileRoles": {
            "shared/forced.sls": "state",
            "states/data.sls": "pillar",
        },
    }
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {"root": str(tmp_path), "profiles": {"default": profile}}
            ]
        },
    )
    roles = {
        "states/a.sls": "state",
        "pillar/a.sls": "pillar",
        "shared/a.sls": "generic",
        "states/top.sls": "state-top",
        "pillar/top.sls": "pillar-top",
        "top.sls": "generic",
        "shared/forced.sls": "state",
        "states/data.sls": "pillar",
        "extensions/evil.py": "generic",
    }
    for relative, role in roles.items():
        path = tmp_path / relative
        client.open(path)
        assert client.context(path)["fileRole"] == role
        symbols = client.symbols(path)
        assert bool(symbols) == (role == "state"), (relative, symbols)
        # State-specific navigation must not reinterpret pillar/ambiguous data.
        if role != "state":
            assert (
                client.request(
                    "textDocument/definition",
                    {
                        "textDocument": {"uri": path.as_uri()},
                        "position": {"line": 2, "character": 5},
                    },
                )
                is None
            )
            completion = client.request(
                "textDocument/completion",
                {
                    "textDocument": {"uri": path.as_uri()},
                    "position": {"line": 1, "character": 7},
                    "context": {"triggerKind": 2, "triggerCharacter": "."},
                },
            )
            assert completion is None or completion["items"] == []
    template = tmp_path / "states/sentinel.sls"
    client.open(
        template, "{{ salt['cmd.run']('touch " + str(sentinel) + "') }}\n"
    )
    client.symbols(template)
    assert not sentinel.exists()


def test_live_profile_switch_invalidates_navigation_without_cross_workspace_leaks(
    tmp_path, clients
):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    for root in (first, second):
        for environment in ("old", "new"):
            directory = root / environment
            directory.mkdir()
            (directory / "target.sls").write_text("target:\n  file.managed\n")
    entries = [
        {
            "workspaceUri": root.as_uri(),
            "root": ".",
            "activeProfile": "old",
            "profiles": {
                name: {
                    "environment": name,
                    "stateRoots": [name],
                    "fileRoles": {"buffer.sls": "state"},
                }
                for name in ("old", "new")
            },
        }
        for root in (first, second)
    ]
    client = clients(
        first, folders=[first, second], settings={"projects": entries}
    )
    text = "include:\n  - target\n\nexample:\n  file.managed:\n    - require:\n      - file: target\n"
    for root in (first, second):
        client.open(root / "buffer.sls", text)

    def definition(root):
        return client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": (root / "buffer.sls").as_uri()},
                "position": {"line": 6, "character": 16},
            },
        )

    assert definition(first)["uri"] == (first / "old/target.sls").as_uri()
    assert definition(second)["uri"] == (second / "old/target.sls").as_uri()
    before_first = client.context(first / "buffer.sls")
    before_second = client.context(second / "buffer.sls")
    entries[0]["activeProfile"] = "new"
    client.notify(
        "workspace/didChangeConfiguration",
        {"settings": {"salt": {"projects": entries}}},
    )
    after = client.context(first / "buffer.sls")
    assert after["activeProfile"] == "new"
    assert after["revision"] > before_first["revision"]
    assert client.context(second / "buffer.sls") == before_second
    assert definition(first)["uri"] == (first / "new/target.sls").as_uri()
    assert definition(second)["uri"] == (second / "old/target.sls").as_uri()
    # Reclassification takes effect for an already-open unsaved buffer.
    entries[0]["profiles"]["new"]["fileRoles"]["buffer.sls"] = "pillar"
    client.notify(
        "workspace/didChangeConfiguration",
        {"settings": {"salt": {"projects": entries}}},
    )
    assert client.symbols(first / "buffer.sls") == []
    assert definition(first) is None
    assert "example" in [
        symbol["name"] for symbol in client.symbols(second / "buffer.sls")
    ]


def test_invalid_configuration_reports_path_and_keeps_last_good_project(
    tmp_path, clients
):
    good = {
        "projects": [
            {
                "root": str(tmp_path),
                "profiles": {"default": {"stateRoots": ["."]}},
            }
        ]
    }
    client = clients(tmp_path, settings=good)
    document = tmp_path / "a.sls"
    client.open(document)
    before = client.context(document)
    client.notify(
        "workspace/didChangeConfiguration", {"settings": {"unrelated": True}}
    )
    assert client.context(document) == before
    bad_profiles = [
        {"stateRoots": "not-an-array"},
        {"saltVersion": 3008},
        {"saltVersion": "9999"},
        {"fileRoles": {"a.sls": "guess"}},
        {"environment": ""},
        {"stateRots": ["."]},
    ]
    for bad in bad_profiles:
        client.notifications.clear()
        client.notify(
            "workspace/didChangeConfiguration",
            {
                "settings": {
                    "salt": {
                        "projects": [
                            {
                                "root": str(tmp_path),
                                "profiles": {"default": bad},
                            }
                        ]
                    }
                }
            },
        )
        assert client.context(document) == before
        assert client.symbols(document)[0]["name"] == "example"
        assert any(
            n["method"] == "window/showMessage"
            and "salt.projects[0]" in n["params"]["message"]
            for n in client.notifications
        )
    for malformed in (
        None,
        [],
        {"projects": "bad"},
        {"projects": [None]},
        {"projects": [{"root": str(tmp_path), "activeProfile": "missing"}]},
    ):
        client.notify(
            "workspace/didChangeConfiguration",
            {"settings": {"salt": malformed}},
        )
        assert client.context(document) == before
    for root in (
        "file:relative",
        "https://example.com/project",
        "file:///tmp/project?query",
    ):
        client.notify(
            "workspace/didChangeConfiguration",
            {"settings": {"salt": {"projects": [{"root": root}]}}},
        )
        assert client.context(document) == before
    # A corrected snapshot applies without restarting.
    good["projects"][0]["profiles"]["default"]["environment"] = "fixed"
    client.notify(
        "workspace/didChangeConfiguration", {"settings": {"salt": good}}
    )
    assert client.context(document)["environment"] == "fixed"


def test_invalid_initial_settings_do_not_disable_other_workspaces(
    tmp_path, clients
):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    client = clients(
        first,
        folders=[first, second],
        settings={
            "projects": [
                {"workspaceUri": first.as_uri(), "activeProfile": "unknown"},
                {
                    "workspaceUri": second.as_uri(),
                    "profiles": {"default": {"stateRoots": ["."]}},
                },
            ]
        },
    )
    client.open(first / "a.sls")
    client.open(second / "a.sls")
    assert client.symbols(first / "a.sls") == []
    assert client.symbols(second / "a.sls")[0]["name"] == "example"
    assert any(
        n["method"] == "window/showMessage" for n in client.notifications
    )


def test_profiles_without_explicit_root_follow_nearest_git_and_folder_changes(
    tmp_path, clients
):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: ../unused\n")
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "workspaceUri": tmp_path.as_uri(),
                    "profiles": {"default": {"stateRoots": ["states"]}},
                }
            ]
        },
    )
    context = client.context(nested / "states/a.sls")
    assert context["projectRoot"] == nested.as_uri()
    assert context["stateRoots"] == [(nested / "states").as_uri()]
    assert context["fileRole"] == "state"
    # Empty snapshot removes explicit configuration; no stale roles remain.
    client.notify(
        "workspace/didChangeConfiguration", {"settings": {"salt": {}}}
    )
    assert client.context(nested / "states/a.sls")["fileRole"] == "generic"
    outside = tmp_path.parent / (tmp_path.name + "-external")
    inner = outside / "nested workspace"
    inner.mkdir(parents=True)
    client.notify(
        "workspace/didChangeWorkspaceFolders",
        {
            "event": {
                "added": [
                    {"uri": p.as_uri(), "name": p.name}
                    for p in (outside, inner)
                ],
                "removed": [],
            }
        },
    )
    assert client.context(inner / "a.sls")["projectRoot"] == inner.as_uri()
    client.notify(
        "workspace/didChangeWorkspaceFolders",
        {
            "event": {
                "added": [],
                "removed": [{"uri": inner.as_uri(), "name": inner.name}],
            }
        },
    )
    assert client.context(inner / "a.sls")["projectRoot"] == outside.as_uri()


def test_include_completion_uses_only_active_state_roots_not_top_or_project_root(
    tmp_path, clients
):
    for directory in ("states", "pillar", "other"):
        (tmp_path / directory).mkdir()
    (tmp_path / "top.sls").write_text("base: {}\n")
    (tmp_path / "states/known.sls").write_text("known:\n  file.managed\n")
    (tmp_path / "pillar/secret.sls").write_text("data: true\n")
    (tmp_path / "other/unrelated.sls").write_text("unrelated: true\n")
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": str(tmp_path),
                    "profiles": {
                        "default": {
                            "stateRoots": ["states"],
                            "pillarRoots": ["pillar"],
                        }
                    },
                }
            ]
        },
    )
    document = tmp_path / "states/buffer.sls"
    client.open(document, "include:\n  - \n")
    result = client.request(
        "textDocument/completion",
        {
            "textDocument": {"uri": document.as_uri()},
            "position": {"line": 1, "character": 4},
        },
    )
    assert result["items"] == [
        {
            "label": "known",
            "textEdit": {
                "range": {
                    "start": {"line": 1, "character": 4},
                    "end": {"line": 1, "character": 4},
                },
                "newText": "known",
            },
        }
    ]


def test_explicit_external_content_roots_keep_project_identity(
    tmp_path, clients
):
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    client = clients(
        project,
        settings={
            "projects": [
                {
                    "root": str(project),
                    "profiles": {
                        "default": {
                            "saltVersion": "3007",
                            "stateRoots": [str(external / "states")],
                            "pillarRoots": [str(external / "pillar")],
                            "moduleDirs": [str(external / "modules")],
                        }
                    },
                }
            ]
        },
    )
    for relative, role in (
        ("states/a.sls", "state"),
        ("pillar/a.sls", "pillar"),
        ("modules/a.py", "generic"),
    ):
        context = client.context(external / relative)
        assert context["projectRoot"] == project.as_uri()
        assert context["saltVersion"] == "3007"
        assert context["fileRole"] == role
    assert (
        client.request("salt/projectContext", {"uri": "untitled:buffer"})
        is None
    )
