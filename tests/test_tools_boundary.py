"""Trust, configuration and process-boundary acceptance for external tools.

Controlled executables report what really happened through sentinel files; the
real stdio server is used wherever behaviour is observable through it.
"""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from salt_lsp.project import ProjectContext
from salt_lsp.tool_process import ProcessRunner
from salt_lsp.tools import ToolService
from test_projects import clients  # pylint: disable=unused-import
from test_tools import executable


def approve(client, params):
    """The editor's user gesture: inspect the subject, then allow its token."""
    status = client.request("salt/toolStatus", params)
    return client.request(
        "salt/authorizeTool",
        {**params, "token": status["token"], "action": "allow"},
    )


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def process_gone(pid):
    """A zombie has stopped executing, whoever is left to reap it."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


def touching(marker):
    return f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"


def state_project(root):
    """Static features need a state root; pillar-safe defaults stay generic."""
    return {"root": str(root), "profiles": {"default": {"stateRoots": ["."]}}}


def single_tool(root, binary, name="yamlfmt", **entry):
    return {
        "projects": [state_project(root)],
        "tools": [
            {
                "projectRoot": str(root),
                "name": name,
                "executable": binary,
                **entry,
            }
        ],
    }


def tool_params(root, tool="yamlfmt"):
    return {"uri": (root / "a.sls").as_uri(), "tool": tool}


async def approved_service(root, **entry):
    service = ToolService(lambda _: ProjectContext(root), lambda: [root])
    assert (
        service.configure(
            [{"projectRoot": str(root), "name": "yamlfmt", **entry}]
        )
        == []
    )
    uri = (root / "a.sls").as_uri()
    subject = service.inspect(uri, "yamlfmt")
    assert (
        service.authorize(uri, "yamlfmt", subject["token"], "allow")["status"]
        == "ready"
    )
    return service, uri


def run_tool(service, uri, invocation=lambda selection, config: [], **kwargs):
    return service.run(
        uri,
        "yamlfmt",
        invocation,
        version=None,
        current_version=lambda: None,
        **kwargs,
    )


def sleeping_tool(ready):
    return f"""
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
while True: time.sleep(1)
"""


def run_runner(binary, tmp_path, *args, **kwargs):
    async def exercise():
        return await ProcessRunner().run(
            binary, list(args), cwd=tmp_path, env=os.environ.copy(), **kwargs
        )

    return asyncio.run(exercise())


# Lifecycle at the stdio boundary


@pytest.mark.parametrize("action", ["exit", "stdin-eof", "sigterm"])
def test_stdio_abrupt_server_end_stops_owned_process(
    tmp_path, clients, action
):
    ready = tmp_path / "pid"
    binary = executable(tmp_path / "yamlfmt", sleeping_tool(ready))
    client = clients(tmp_path, settings=single_tool(tmp_path, binary))
    params = tool_params(tmp_path)
    approve(client, params)
    client.send({"id": 1000, "method": "salt/toolProbe", "params": params})
    wait_until(ready.exists)
    pid = int(ready.read_text())
    assert not process_gone(pid)
    if action == "exit":
        client.notify("exit", {})
    elif action == "stdin-eof":
        client.process.stdin.close()
    else:
        client.process.terminate()
    client.process.wait(timeout=5)
    wait_until(lambda: process_gone(pid))


def test_static_requests_answer_while_a_tool_runs(tmp_path, clients):
    ready = tmp_path / "pid"
    binary = executable(tmp_path / "yamlfmt", sleeping_tool(ready))
    client = clients(tmp_path, settings=single_tool(tmp_path, binary))
    document = tmp_path / "a.sls"
    client.open(document)
    params = tool_params(tmp_path)
    approve(client, params)
    client.send({"id": 1000, "method": "salt/toolProbe", "params": params})
    wait_until(ready.exists)
    assert client.symbols(document)


# Bounded process execution


@pytest.mark.parametrize("repeat", range(10))
def test_runner_keeps_all_output_written_before_exit(tmp_path, repeat):
    """A clean result must never be a silently truncated prefix."""
    size = 60000 + repeat
    binary = executable(
        tmp_path / "tool",
        f"""
import os
os.write(1, b"o" * {size})
os.write(2, b"e" * {size})
os._exit(0)
""",
    )
    result = run_runner(binary, tmp_path)
    assert result.status == "clean"
    assert len(result.stdout) == size
    assert len(result.stderr) == size


def test_runner_keeps_output_when_exit_and_data_arrive_together(tmp_path):
    """A stalled loop sees exit and pipe data at once; neither may lose data."""
    started = tmp_path / "started"
    binary = executable(
        tmp_path / "tool",
        f"""
import os, time
from pathlib import Path
Path({str(started)!r}).touch()
time.sleep(.05)
os.write(1, b"x" * 60000)
os._exit(0)
""",
    )

    async def exercise():
        task = asyncio.create_task(
            ProcessRunner().run(
                binary, [], cwd=tmp_path, env=os.environ.copy()
            )
        )
        while not started.exists():
            await asyncio.sleep(0.001)
        time.sleep(0.3)  # Stalls the loop on purpose.
        return await task

    result = asyncio.run(exercise())
    assert result.status == "clean"
    assert len(result.stdout) == 60000


def test_runner_loop_teardown_still_stops_the_group(tmp_path):
    """asyncio.run cancels every task once; cleanup must survive that."""
    ready = tmp_path / "pid"
    binary = executable(tmp_path / "tool", sleeping_tool(ready))

    async def exercise():
        asyncio.ensure_future(
            ProcessRunner().run(
                binary, [], cwd=tmp_path, env=os.environ.copy(), timeout=30
            )
        )
        while not ready.exists():
            await asyncio.sleep(0.005)

    asyncio.run(exercise())
    assert process_gone(int(ready.read_text()))


def test_runner_escaped_descendant_is_a_bounded_cleanup_failure(tmp_path):
    ready = tmp_path / "pid"
    binary = executable(
        tmp_path / "tool",
        f"""
import os, time
from pathlib import Path
if os.fork() == 0:
    os.setsid()
    Path({str(ready)!r}).write_text(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
while not Path({str(ready)!r}).exists(): time.sleep(.001)
print("partial")
""",
    )
    started = time.monotonic()
    try:
        result = run_runner(binary, tmp_path)
    finally:
        if ready.exists():
            os.kill(int(ready.read_text()), signal.SIGKILL)
    assert result.status == "cleanup-failure"
    assert time.monotonic() - started < 3


def test_runner_stdin_contract(tmp_path):
    """EOF reaches a reader, early exit is survivable, input is bounded."""
    payload = b"x" * (2 * 1024 * 1024)
    counted = run_runner(
        sys.executable,
        tmp_path,
        "-c",
        "import sys; print(len(sys.stdin.buffer.read()))",
        data=payload,
    )
    assert counted.status == "clean"
    assert counted.stdout == b"%d\n" % len(payload)
    early = run_runner(
        sys.executable, tmp_path, "-c", "import os; os._exit(0)", data=payload
    )
    assert early.status == "clean"
    marker = tmp_path / "ran"
    limited = run_runner(
        sys.executable,
        tmp_path,
        "-c",
        f"open({str(marker)!r}, 'w')",
        data=b"x" * (4 * 1024 * 1024 + 1),
    )
    assert limited.status == "input-limit"
    assert not marker.exists()


def test_runner_floods_on_both_streams_are_bounded(tmp_path):
    binary = executable(
        tmp_path / "tool",
        """
import os, threading
chunk = b"x" * 65536
def flood(fd):
    while True:
        os.write(fd, chunk)
threading.Thread(target=flood, args=(2,), daemon=True).start()
flood(1)
""",
    )
    result = run_runner(binary, tmp_path, output_limit=1024)
    assert result.status == "output-limit"
    assert len(result.stdout) <= 1024
    assert len(result.stderr) <= 1024


@pytest.mark.parametrize("ticks", [0, 1, 2, 3, 5, 8, 13, 21])
def test_runner_cancel_near_spawn_leaves_no_process(tmp_path, ticks):
    ready = tmp_path / "pid"
    binary = executable(tmp_path / "tool", sleeping_tool(ready))

    async def exercise():
        runner = ProcessRunner()
        task = asyncio.create_task(
            runner.run(binary, [], cwd=tmp_path, env=os.environ.copy())
        )
        for _ in range(ticks):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        follow_up = await runner.run(
            sys.executable, ["-c", ""], cwd=tmp_path, env=os.environ.copy()
        )
        assert follow_up.status == "clean"

    asyncio.run(exercise())
    time.sleep(0.1)
    if ready.exists():
        assert process_gone(int(ready.read_text()))


@pytest.mark.parametrize(
    "shebang", ["#!/nonexistent/interpreter\n"], ids=["bad-interpreter"]
)
def test_spawn_failure_is_reported_and_the_session_continues(
    tmp_path, clients, shebang
):
    binary = tmp_path / "yamlfmt"
    binary.write_text(shebang)
    binary.chmod(0o755)
    client = clients(tmp_path, settings=single_tool(tmp_path, str(binary)))
    document = tmp_path / "a.sls"
    client.open(document)
    params = tool_params(tmp_path)
    approve(client, params)
    result = client.request("salt/toolProbe", params)
    assert result["status"] == "spawn-failure"
    assert "interpreter" in result["message"]
    assert client.symbols(document)


def test_external_execution_refused_on_unsupported_platform(
    tmp_path, monkeypatch
):
    marker = tmp_path / "ran"
    monkeypatch.setattr("salt_lsp.tool_process.sys.platform", "win32")
    result = run_runner(
        sys.executable, tmp_path, "-c", f"open({str(marker)!r}, 'w')"
    )
    assert result.status == "unsupported-platform"
    assert not marker.exists()


# The provider seam used by lint and format


def test_provider_caller_cancellation_is_not_swallowed(tmp_path):
    """A timeout around run() must fire, and a cancelled task must stay so."""
    ready = tmp_path / "pid"
    binary = executable(tmp_path / "yamlfmt", sleeping_tool(ready))

    async def exercise():
        service, uri = await approved_service(tmp_path, executable=binary)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await run_tool(service, uri, timeout=30)
        assert process_gone(int(ready.read_text()))
        ready.unlink()
        task = asyncio.create_task(run_tool(service, uri, timeout=30))
        while not ready.exists():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process_gone(int(ready.read_text()))
        await service.close()

    asyncio.run(exercise())


def test_provider_gate_is_actionable_and_builds_no_invocation(tmp_path):
    marker = tmp_path / "ran"
    binary = executable(tmp_path / "yamlfmt", touching(marker))
    called = []

    async def exercise():
        service = ToolService(
            lambda _: ProjectContext(tmp_path), lambda: [tmp_path]
        )
        service.configure(single_tool(tmp_path, binary)["tools"])
        return await run_tool(
            service,
            (tmp_path / "a.sls").as_uri(),
            lambda selection, config: called.append(1) or [],
        )

    result = asyncio.run(exercise())
    assert result.status == "denied-trust"
    assert "approval" in result.message
    assert not called
    assert not marker.exists()


@pytest.mark.parametrize(
    "exit_code,status,fragment",
    [(0, "clean", "completed"), (3, "tool-failure", "exited")],
)
def test_provider_results_carry_sanitized_messages(
    tmp_path, exit_code, status, fragment
):
    binary = executable(
        tmp_path / "yamlfmt",
        f"import sys\nprint('SECRET_OUTPUT')\nsys.exit({exit_code})\n",
    )

    async def exercise():
        service, uri = await approved_service(tmp_path, executable=binary)
        result = await run_tool(service, uri)
        await service.close()
        return result

    result = asyncio.run(exercise())
    assert result.status == status
    assert result.returncode == exit_code
    assert result.stdout == b"SECRET_OUTPUT\n"
    assert fragment in result.message
    assert "SECRET" not in result.message


def test_provider_config_edit_stops_a_running_job(tmp_path):
    ready = tmp_path / "pid"
    config = tmp_path / "selected.yaml"
    config.write_text("a: 1\n")
    binary = executable(tmp_path / "yamlfmt", sleeping_tool(ready))

    async def exercise():
        service, uri = await approved_service(
            tmp_path, executable=binary, config="selected.yaml"
        )
        task = asyncio.create_task(run_tool(service, uri, timeout=30))
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.005)
            config.write_text("a: 2\n")
            result = await task
        assert result.status in ("cancelled", "stale")
        assert result.stdout == b""
        assert service.inspect(uri, "yamlfmt")["status"] == "denied-trust"
        await service.close()

    asyncio.run(exercise())
    assert process_gone(int(ready.read_text()))


# Configuration boundary


def test_launch_uses_owned_config_and_scrubbed_environment(
    tmp_path, monkeypatch
):
    project = tmp_path / "ancestor" / "project"
    (project / "bin").mkdir(parents=True)
    installed = tmp_path / "installed"
    installed.mkdir()
    # Native discovery candidates that must never be selected on their own.
    for native in (
        tmp_path / "ancestor" / ".salt-lint",
        tmp_path / "ancestor" / ".yamlfmt",
        project / ".salt-lint",
        project / ".yamlfmt",
    ):
        native.write_text("SENTINEL: native-config\n")
    report = tmp_path / "report.json"
    binary = executable(
        installed / "yamlfmt",
        f"""
import json, os, sys
config = sys.argv[1]
with open({str(report)!r}, "w") as stream:
    json.dump([config, open(config).read(), dict(os.environ), os.getcwd()], stream)
""",
    )
    monkeypatch.setenv("PATH", f"{project}/bin:rel:{installed}")
    monkeypatch.setenv("PYTHONPATH", str(project))
    monkeypatch.setenv("PYTHONWARNINGS", "workspace.Category")
    monkeypatch.setenv("KEEP_ME", "kept")

    async def exercise():
        service, uri = await approved_service(project, executable=binary)
        assert service.inspect(uri, "yamlfmt")["selection"]["config"] is None
        result = await run_tool(
            service, uri, lambda selection, config: [str(config)]
        )
        assert result.status == "clean"
        await service.close()

    asyncio.run(exercise())
    config, content, env, cwd = json.loads(report.read_text())
    assert content == "{}\n"
    assert not Path(config).exists()
    assert not Path(config).is_relative_to(tmp_path / "ancestor")
    assert cwd == str(project)
    assert env["HOME"] == str(Path(config).parent)
    assert env["XDG_CONFIG_HOME"] == str(Path(config).parent)
    assert env["PATH"] == str(installed)
    assert env["KEEP_ME"] == "kept"
    assert "PYTHONPATH" not in env
    assert "PYTHONWARNINGS" not in env


@pytest.mark.parametrize("kind", ["config", "rules"])
def test_symlink_escape_needs_explicit_override_then_new_approval(
    tmp_path, clients, kind
):
    project = tmp_path / "project"
    project.mkdir()
    binary = executable(tmp_path / "salt-lint", "")
    if kind == "config":
        shared = tmp_path / "shared.yaml"
        shared.write_text("{}\n")
        (project / "selected.yaml").symlink_to(shared)
        selection = {"config": "selected.yaml"}
    else:
        shared = tmp_path / "shared-rules"
        shared.mkdir()
        (project / "rules").symlink_to(shared)
        selection = {"ruleDirs": ["rules"]}
    tools = single_tool(project, binary, "salt-lint", **selection)["tools"]
    client = clients(project, settings={"tools": tools})
    params = tool_params(project, "salt-lint")
    denied = client.request("salt/toolStatus", params)
    assert denied["status"] == "invalid-config"
    assert "allowSharedConfig" in denied["message"]
    client.notify(
        "workspace/didChangeConfiguration",
        {
            "settings": {
                "salt": {"tools": [{**tools[0], "allowSharedConfig": True}]}
            }
        },
    )
    status = client.request("salt/toolStatus", params)
    assert status["status"] == "denied-trust"
    shown = status["selection"]
    assert str(shared) in (
        [shown["config"]] if kind == "config" else shown["ruleDirs"]
    )


@pytest.mark.parametrize(
    "setup",
    ["missing", "malformed", "not-a-mapping", "oversized", "directory"],
)
def test_invalid_selected_config_is_actionable_and_static_survives(
    tmp_path, clients, setup
):
    marker = tmp_path / "executed"
    binary = executable(tmp_path / "yamlfmt", touching(marker))
    config = tmp_path / "selected.yaml"
    if setup == "malformed":
        config.write_text("a: [\n")
    elif setup == "not-a-mapping":
        config.write_text("- a\n")
    elif setup == "oversized":
        config.write_text("# pad\n" * 20000)
    elif setup == "directory":
        config.mkdir()
    client = clients(
        tmp_path,
        settings=single_tool(tmp_path, binary, config="selected.yaml"),
    )
    document = tmp_path / "a.sls"
    client.open(document)
    params = tool_params(tmp_path)
    status = client.request("salt/toolStatus", params)
    assert status["status"] == "invalid-config"
    assert status["message"]
    assert "token" not in status
    assert (
        client.request("salt/toolProbe", params)["status"] == "invalid-config"
    )
    assert client.symbols(document)
    assert not marker.exists()


# Custom rules and trust


def test_custom_rules_never_imported_by_server_or_unverified_adapter(
    tmp_path, clients
):
    """Sentinels cover the binary and every rule file, including at import."""
    marker = tmp_path / "executed"
    binary_marker = tmp_path / "binary-executed"
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "__init__.py").write_text(touching(marker))
    (rules / "custom.py").write_text(touching(marker))
    (tmp_path / ".salt-lint").write_text("rulesdir: [rules]\n")
    binary = executable(tmp_path / "salt-lint", touching(binary_marker))
    client = clients(
        tmp_path,
        settings=single_tool(
            tmp_path,
            binary,
            "salt-lint",
            config=".salt-lint",
            ruleDirs=["rules"],
        ),
    )
    document = tmp_path / "a.sls"
    client.open(document)
    params = tool_params(tmp_path, "salt-lint")
    status = client.request("salt/toolStatus", params)
    assert status["status"] == "denied-trust"
    assert status["selection"]["ruleDirs"] == [str(rules)]
    assert status["selection"]["config"] == str(tmp_path / ".salt-lint")
    assert client.request("salt/toolProbe", params)["status"] == "denied-trust"
    assert client.symbols(document)
    assert not marker.exists()
    assert not binary_marker.exists()
    assert approve(client, params)["status"] == "ready"
    assert (
        client.request("salt/toolProbe", params)["status"]
        == "unsupported-capability"
    )
    assert not marker.exists()
    # A requested probe now executes the explicitly trusted binary. The server
    # does not import custom rules or fall back to a Python bridge on failure.
    assert binary_marker.exists()


@pytest.mark.parametrize("change", ["add", "remove", "rename", "symlink"])
def test_rule_tree_changes_invalidate_approval(tmp_path, clients, change):
    rules = tmp_path / "rules"
    rules.mkdir()
    rule = rules / "custom.py"
    rule.write_text("# rule\n")
    binary = executable(tmp_path / "salt-lint", "")
    client = clients(
        tmp_path,
        settings=single_tool(
            tmp_path, binary, "salt-lint", ruleDirs=["rules"]
        ),
    )
    params = tool_params(tmp_path, "salt-lint")
    assert approve(client, params)["status"] == "ready"
    if change == "add":
        (rules / "other.py").write_text("# new\n")
    elif change == "remove":
        rule.unlink()
    elif change == "rename":
        rule.rename(rules / "renamed.py")
    else:
        (rules / "link.py").symlink_to(rule)
    expected = "invalid-config" if change == "symlink" else "denied-trust"
    assert client.request("salt/toolStatus", params)["status"] == expected


def test_explicit_workspace_tool_is_labelled_and_still_needs_approval(
    tmp_path, clients
):
    marker = tmp_path / "ran"
    binary = executable(tmp_path / "yamlfmt", touching(marker))
    client = clients(tmp_path, settings=single_tool(tmp_path, binary))
    status = client.request("salt/toolStatus", tool_params(tmp_path))
    assert status["status"] == "denied-trust"
    assert status["selection"]["workspaceExecutable"] is True
    assert status["selection"]["executableSource"] == "explicit"
    assert not marker.exists()


def test_approval_token_does_not_transfer_between_projects(tmp_path, clients):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    binary = executable(tmp_path / "yamlfmt", "")
    client = clients(
        first,
        folders=[first, second],
        settings={
            "tools": [
                single_tool(root, binary)["tools"][0]
                for root in (first, second)
            ]
        },
    )
    first_params = tool_params(first)
    second_params = tool_params(second)
    token = client.request("salt/toolStatus", first_params)["token"]
    assert (
        client.request(
            "salt/authorizeTool",
            {**second_params, "token": token, "action": "allow"},
        )["status"]
        == "denied-trust"
    )
    assert approve(client, first_params)["status"] == "ready"
    assert (
        client.request("salt/toolStatus", second_params)["status"]
        == "denied-trust"
    )


# Executable discovery


@pytest.mark.parametrize(
    "setup",
    [
        "unselected",
        "explicit-missing",
        "not-executable",
        "directory",
        "path-empty",
    ],
)
def test_missing_tool_is_actionable_and_static_survives(
    tmp_path, clients, monkeypatch, setup
):
    project = tmp_path / "project"
    project.mkdir()
    entry = {"projectRoot": str(project), "name": "yamlfmt"}
    if setup == "explicit-missing":
        entry["executable"] = str(tmp_path / "absent")
    elif setup == "not-executable":
        plain = tmp_path / "plain"
        plain.write_text("")
        entry["executable"] = str(plain)
    elif setup == "directory":
        entry["executable"] = str(tmp_path)
    elif setup == "path-empty":
        monkeypatch.setenv("PATH", str(tmp_path / "absent-bin"))
    client = clients(
        project,
        settings={
            "projects": [state_project(project)],
            "tools": [] if setup == "unselected" else [entry],
        },
    )
    document = project / "a.sls"
    client.open(document)
    params = tool_params(project)
    status = client.request("salt/toolStatus", params)
    assert status["status"] == "missing-tool"
    assert status["message"]
    assert client.request("salt/toolProbe", params)["status"] == "missing-tool"
    assert client.symbols(document)


def test_discovery_rejects_other_folders_and_configured_projects(
    tmp_path, clients, monkeypatch
):
    project = tmp_path / "project"
    other_folder = tmp_path / "folder"
    configured = tmp_path / "configured"
    installed = tmp_path / "installed"
    for directory in (project, installed):
        directory.mkdir()
    for root in (other_folder, configured):
        (root / "bin").mkdir(parents=True)
        executable(root / "bin" / "yamlfmt", "raise RuntimeError('UNSAFE')\n")
    safe = executable(installed / "yamlfmt", "")
    monkeypatch.setenv(
        "PATH", f"{other_folder}/bin:{configured}/bin:{installed}"
    )
    client = clients(
        project,
        folders=[project, other_folder],
        settings={
            "projects": [state_project(configured)],
            "tools": [{"projectRoot": str(project), "name": "yamlfmt"}],
        },
    )
    status = client.request("salt/toolStatus", tool_params(project))
    assert status["selection"]["executable"] == safe


def test_discovery_sees_through_dotdot_after_symlink(
    tmp_path, clients, monkeypatch
):
    """`link/..` is the parent of the link's target, not of the link."""
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    executable(project / "yamlfmt", "raise RuntimeError('UNSAFE')\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link").symlink_to(project / "sub")
    installed = tmp_path / "installed"
    installed.mkdir()
    safe = executable(installed / "yamlfmt", "")
    monkeypatch.setenv("PATH", f"{outside}/link/..:{installed}")
    client = clients(
        project,
        settings={"tools": [{"projectRoot": str(project), "name": "yamlfmt"}]},
    )
    status = client.request("salt/toolStatus", tool_params(project))
    assert status["selection"]["executable"] == safe


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unreadable_path_entry_does_not_break_selection_or_launch(
    tmp_path, monkeypatch
):
    locked = tmp_path / "locked"
    (locked / "bin").mkdir(parents=True)
    installed = tmp_path / "installed"
    installed.mkdir()
    binary = executable(
        installed / "yamlfmt", "import os\nprint(os.environ['PATH'])\n"
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("PATH", f"{locked}/bin:{installed}")

    async def exercise():
        service, uri = await approved_service(project)
        selection = service.inspect(uri, "yamlfmt")["selection"]
        assert selection["executable"] == binary
        result = await run_tool(service, uri)
        await service.close()
        return result

    locked.chmod(0)
    try:
        result = asyncio.run(exercise())
    finally:
        locked.chmod(0o755)
    assert result.status == "clean"
    assert result.stdout.decode().strip() == str(installed)


# Status and logging


def test_debug_logs_omit_identifiers_and_edits_from_documents(
    tmp_path, clients
):
    log = tmp_path / "server.log"
    document = tmp_path / "a.sls"
    source = (
        "SECRET_ID:\n  file.managed:\n    - require:\n"
        "      - file: SECRET_REFERENCE\n"
    )
    with log.open("wb") as stream:
        client = clients(
            tmp_path,
            stderr=stream,
            settings={"projects": [state_project(tmp_path)]},
        )
        client.open(document, source)
        for version, text in ((2, "SECRET_EDIT: {\n"), (3, source)):
            client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {
                        "uri": document.as_uri(),
                        "version": version,
                    },
                    "contentChanges": [{"text": text}],
                },
            )
            client.symbols(document)
        client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": document.as_uri()},
                "position": {"line": 3, "character": 20},
            },
        )
        client.close()
    assert b"SECRET_" not in log.read_bytes()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"uri": "not a uri", "tool": "yamlfmt"},
        {"uri": 5, "tool": "yamlfmt"},
        {"uri": "file:///tmp/a.sls", "tool": 5},
    ],
)
@pytest.mark.parametrize(
    "method", ["salt/toolStatus", "salt/toolProbe", "salt/authorizeTool"]
)
def test_malformed_tool_requests_keep_session_usable(
    tmp_path, clients, method, params
):
    client = clients(
        tmp_path, settings={"projects": [state_project(tmp_path)]}
    )
    client.send({"id": 1000, "method": method, "params": params})
    while client.messages.get(timeout=5).get("id") != 1000:
        pass
    document = tmp_path / "a.sls"
    client.open(document)
    assert client.symbols(document)
