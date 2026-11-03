"""External-tool acceptance through the real stdio server."""

import sys
import json
import asyncio
import os
import time
from pathlib import Path

import pytest

from salt_lsp.tool_process import ProcessRunner
from salt_lsp.tools import ToolService
from salt_lsp.project import ProjectContext
from test_projects import clients  # pylint: disable=unused-import


def test_real_yamlfmt_compatibility_and_explicit_config_boundary(
    tmp_path, clients
):
    binary = os.environ.get("SALT_LSP_TEST_YAMLFMT")
    if not binary:
        pytest.skip(
            "Set SALT_LSP_TEST_YAMLFMT to a user-installed yamlfmt 0.21.0"
        )
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".yamlfmt").write_text("formatter:\n  indent: 8\n")
    config = project / "selected.yaml"
    config.write_text("formatter:\n  type: basic\n  indent: 2\n")
    entry = {
        "projectRoot": str(project),
        "name": "yamlfmt",
        "executable": binary,
        "config": "selected.yaml",
    }
    client = clients(project, settings={"tools": [entry]})
    uri = (project / "a.sls").as_uri()
    params = {"uri": uri, "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    assert client.request("salt/toolProbe", params)["status"] == "clean"

    async def exercise():
        service = ToolService(
            lambda _: ProjectContext(project), lambda: [project]
        )
        assert service.configure([entry]) == []
        subject = service.inspect(uri, "yamlfmt")
        service.authorize(uri, "yamlfmt", subject["token"], "allow")
        result = await service.run(
            uri,
            "yamlfmt",
            lambda selection, path: [
                "-conf",
                str(path),
                "-no_global_conf",
                "-in",
            ],
            version=1,
            current_version=lambda: 1,
            data=b"key:\n    nested: value\n",
        )
        assert result.status == "clean"
        assert result.stdout == b"key:\n  nested: value\n"
        await service.close()

    asyncio.run(exercise())


def executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


def test_runner_bounds_queue_and_rechecks_permission_after_wait(tmp_path):
    release = tmp_path / "release"
    binary = executable(
        tmp_path / "tool",
        f"""
import sys, time
from pathlib import Path
Path({str(tmp_path)!r}, 'ready-' + sys.argv[1]).touch()
while not Path({str(release)!r}).exists(): time.sleep(.005)
""",
    )

    async def exercise():
        allowed = True
        runner = ProcessRunner()
        tasks = [
            asyncio.create_task(
                runner.run(
                    binary,
                    [str(i)],
                    cwd=tmp_path,
                    env=os.environ.copy(),
                    valid=lambda: allowed,
                )
            )
            for i in range(11)
        ]
        async with asyncio.timeout(3):
            while len(list(tmp_path.glob("ready-*"))) < 2:
                await asyncio.sleep(0.005)
        assert len(list(tmp_path.glob("ready-*"))) == 2
        allowed = False
        release.touch()
        results = await asyncio.gather(*tasks)
        statuses = [r.status for r in results]
        assert statuses.count("clean") == 2
        assert statuses.count("stale") == 8
        assert statuses.count("busy") == 1
        assert len(list(tmp_path.glob("ready-*"))) == 2

    asyncio.run(exercise())


def test_runner_timeout_nonreader_is_bounded_and_next_job_works(tmp_path):
    binary = executable(tmp_path / "tool", "import time\ntime.sleep(60)\n")

    async def exercise():
        runner = ProcessRunner()
        async with asyncio.timeout(2):
            result = await runner.run(
                binary,
                [],
                cwd=tmp_path,
                env=os.environ.copy(),
                data=b"x" * 1000000,
                timeout=0.1,
            )
        assert result.status == "timeout"
        result = await runner.run(
            sys.executable,
            ["-c", 'print("ok")'],
            cwd=tmp_path,
            env=os.environ.copy(),
        )
        assert result.status == "clean"
        assert result.stdout == b"ok\n"

    asyncio.run(exercise())


def test_provider_document_staleness_cancels_and_discards_output(tmp_path):
    ready = tmp_path / "ready"
    binary = executable(
        tmp_path / "yamlfmt",
        f"""
import time
from pathlib import Path
Path({str(ready)!r}).touch()
time.sleep(60)
print('MUST NOT PUBLISH')
""",
    )

    async def exercise():
        version = 1
        service = ToolService(
            lambda _: ProjectContext(tmp_path), lambda: [tmp_path]
        )
        service.configure(
            [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ]
        )
        uri = (tmp_path / "a.sls").as_uri()
        subject = service.inspect(uri, "yamlfmt")
        service.authorize(uri, "yamlfmt", subject["token"], "allow")
        task = asyncio.create_task(
            service.run(
                uri,
                "yamlfmt",
                lambda selection, config: [],
                version=version,
                current_version=lambda: version,
            )
        )
        async with asyncio.timeout(2):
            while not ready.exists():
                await asyncio.sleep(0.005)
            version = 2
            result = await task
        assert result.status in ("cancelled", "stale")
        assert result.stdout == b""
        await service.close()

    asyncio.run(exercise())


def test_explicit_binary_does_not_grant_execution(tmp_path, clients):
    marker = tmp_path / "executed"
    binary = executable(
        tmp_path / "yamlfmt",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
    )
    client = clients(
        tmp_path,
        settings={
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ]
        },
    )
    params = {"uri": (tmp_path / "a.sls").as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    assert status["status"] == "denied-trust"
    assert status["selection"]["executable"] == binary
    assert client.request("salt/toolProbe", params)["status"] == "denied-trust"
    assert not marker.exists()
    client.open(tmp_path / "a.sls")
    assert client.context(tmp_path / "a.sls")["saltVersion"] == "3008"


def test_approved_probe_pins_config_and_revokes_on_edit(tmp_path, clients):
    marker = tmp_path / "invocation.json"
    config = tmp_path / "chosen.yaml"
    config.write_text("formatter:\n  type: basic\n")
    binary = executable(
        tmp_path / "yamlfmt",
        f"""
import json, os, sys
from pathlib import Path
Path({str(marker)!r}).write_text(json.dumps([sys.argv[1:], os.getcwd()]))
print('yamlfmt 0.21.0 (test)')
""",
    )
    client = clients(
        tmp_path,
        settings={
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                    "config": "chosen.yaml",
                }
            ]
        },
    )
    params = {"uri": (tmp_path / "a.sls").as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    assert (
        client.request(
            "salt/authorizeTool",
            {**params, "token": status["token"], "action": "allow"},
        )["status"]
        == "ready"
    )
    assert client.request("salt/toolProbe", params)["status"] == "clean"
    args, cwd = json.loads(marker.read_text())
    assert args == ["-conf", str(config), "-no_global_conf", "-version"]
    assert cwd == str(tmp_path)
    config.write_text("formatter:\n  type: kyaml\n")
    assert client.request("salt/toolProbe", params)["status"] == "denied-trust"
    assert (
        client.request(
            "salt/authorizeTool",
            {**params, "token": status["token"], "action": "allow"},
        )["status"]
        == "denied-trust"
    )


@pytest.mark.parametrize("stream", [1, 2])
def test_stdio_flood_is_failure_not_clean_or_document_output(
    tmp_path, clients, stream
):
    binary = executable(
        tmp_path / "yamlfmt",
        f"import os\nos.write({stream}, b'SECRET_CONTENT' * 100000)\n",
    )
    client = clients(
        tmp_path,
        settings={
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ]
        },
    )
    params = {"uri": (tmp_path / "a.sls").as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    result = client.request("salt/toolProbe", params)
    assert result["status"] == "output-limit"
    assert "SECRET_CONTENT" not in json.dumps([result, client.notifications])
    assert client.context(tmp_path / "a.sls") is not None


def test_runner_timeout_kills_descendant_after_leader_exit(tmp_path):
    ready = tmp_path / "child"
    binary = executable(
        tmp_path / "tool",
        f"""
import os, signal, time
from pathlib import Path
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path({str(ready)!r}).write_text(str(os.getpid()))
    while True: time.sleep(1)
else:
    while not Path({str(ready)!r}).exists(): time.sleep(.001)
""",
    )

    async def exercise():
        result = await ProcessRunner().run(
            binary, [], cwd=tmp_path, env=os.environ.copy(), timeout=0.3
        )
        assert result.status == "clean"
        pid = int(ready.read_text())
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"

    asyncio.run(exercise())


def test_runner_repeated_cancel_and_stdin_backpressure(tmp_path):
    ready = tmp_path / "ready"
    binary = executable(
        tmp_path / "tool",
        f"""
import time, signal
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}).touch()
while True: time.sleep(1)
""",
    )

    async def exercise():
        runner = ProcessRunner()
        task = asyncio.create_task(
            runner.run(
                binary,
                [],
                cwd=tmp_path,
                env=os.environ.copy(),
                data=b"x" * 1000000,
            )
        )
        async with asyncio.timeout(2):
            while not ready.exists():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await runner.run(
            sys.executable,
            ["-c", "import sys; print(len(sys.stdin.buffer.read()))"],
            cwd=tmp_path,
            env=os.environ.copy(),
            data=b"abc",
        )
        assert result.status == "clean"
        assert result.stdout == b"3\n"

    asyncio.run(exercise())


def test_discovery_skips_project_and_relative_paths(
    tmp_path, clients, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    executable(project / "yamlfmt", "raise RuntimeError('MUST NOT RUN')\n")
    safe = executable(
        installed / "yamlfmt", "print('yamlfmt 0.21.0 (test)')\n"
    )
    monkeypatch.setenv("PATH", f":.:bin:{project}:{installed}")
    client = clients(
        project,
        settings={"tools": [{"projectRoot": str(project), "name": "yamlfmt"}]},
    )
    status = client.request(
        "salt/toolStatus",
        {"uri": (project / "a.sls").as_uri(), "tool": "yamlfmt"},
    )
    assert status["selection"]["executable"] == safe


def test_shared_config_needs_override_and_separate_approval(tmp_path, clients):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "shared.yaml"
    config.write_text("{}")
    binary = executable(
        tmp_path / "yamlfmt", "print('yamlfmt 0.21.0 (test)')\n"
    )
    entry = {
        "projectRoot": str(project),
        "name": "yamlfmt",
        "executable": binary,
        "config": str(config),
    }
    client = clients(project, settings={"tools": [entry]})
    params = {"uri": (project / "a.sls").as_uri(), "tool": "yamlfmt"}
    assert (
        client.request("salt/toolProbe", params)["status"] == "invalid-config"
    )
    entry["allowSharedConfig"] = True
    client.notify(
        "workspace/didChangeConfiguration",
        {"settings": {"salt": {"tools": [entry]}}},
    )
    assert client.request("salt/toolProbe", params)["status"] == "denied-trust"


def test_stdio_revoke_stops_running_probe_and_static_requests_respond(
    tmp_path, clients
):
    ready = tmp_path / "pid"
    binary = executable(
        tmp_path / "yamlfmt",
        f"""
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
while True: time.sleep(1)
""",
    )
    client = clients(
        tmp_path,
        settings={
            "tools": [
                {
                    "projectRoot": str(tmp_path),
                    "name": "yamlfmt",
                    "executable": binary,
                }
            ]
        },
    )
    params = {"uri": (tmp_path / "a.sls").as_uri(), "tool": "yamlfmt"}
    status = client.request("salt/toolStatus", params)
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    client.send({"id": 1000, "method": "salt/toolProbe", "params": params})
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ready.exists()
    assert client.context(tmp_path / "a.sls") is not None
    assert (
        client.request(
            "salt/authorizeTool",
            {**params, "token": status["token"], "action": "revoke"},
        )["status"]
        == "denied-trust"
    )
    while True:
        message = client.messages.get(timeout=3)
        if message.get("id") == 1000:
            assert message["result"]["status"] in ("cancelled", "stale")
            break
    pid = int(ready.read_text())
    assert not Path(f"/proc/{pid}").exists()
    assert client.request("salt/toolProbe", params)["status"] == "denied-trust"


def test_custom_rules_must_match_selected_config_and_edits_need_new_grant(
    tmp_path, clients
):
    rules = tmp_path / "rules"
    rules.mkdir()
    rule = rules / "custom.py"
    rule.write_text("raise RuntimeError('NEVER IMPORT')")
    config = tmp_path / ".salt-lint"
    config.write_text("rulesdir: [rules]\n")
    binary = executable(
        tmp_path / "salt-lint", "raise RuntimeError('NEVER PROBE STOCK')\n"
    )
    entry = {
        "projectRoot": str(tmp_path),
        "name": "salt-lint",
        "executable": binary,
        "config": str(config),
    }
    client = clients(tmp_path, settings={"tools": [entry]})
    params = {"uri": (tmp_path / "a.sls").as_uri(), "tool": "salt-lint"}
    assert (
        client.request("salt/toolStatus", params)["status"] == "invalid-config"
    )
    entry["ruleDirs"] = ["rules"]
    client.notify(
        "workspace/didChangeConfiguration",
        {"settings": {"salt": {"tools": [entry]}}},
    )
    status = client.request("salt/toolStatus", params)
    assert status["status"] == "denied-trust"
    client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )
    assert (
        client.request("salt/toolProbe", params)["status"]
        == "unsupported-capability"
    )
    rule.write_text("raise RuntimeError('EDITED')")
    assert client.request("salt/toolProbe", params)["status"] == "denied-trust"
