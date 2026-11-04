"""Explicit, non-executing selection and revocable external-tool permission.

No setting grants trust. Editors approve a selection returned by inspect(),
using its opaque token. Providers must use run(), never spawn independently.
"""

from dataclasses import dataclass, replace
import asyncio
import hashlib
import logging
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Callable

import yaml

from salt_lsp.project import ProjectContext
from salt_lsp.tool_process import ProcessRunner, ProcessResult


RESULT_MESSAGES = {
    "clean": "Tool completed.",
    "timeout": "Tool exceeded its execution deadline and was stopped.",
    "input-limit": "The document exceeds the 4 MiB tool input budget; the tool was not started.",
    "output-limit": "Tool exceeded the output budget and was stopped; no partial result is usable.",
    "spawn-failure": "Could not start the selected tool; check its interpreter, permissions and dependencies.",
    "execution-failure": "Communication with the tool failed; no result is usable.",
    "tool-failure": "Tool exited unsuccessfully; check the selected configuration and installed dependencies.",
    "cancelled": "Tool cancelled and cleanup completed; request fresh results if needed.",
    "stale": "Project, permission, config or document changed; request fresh results.",
    "busy": "Tool queue is full; retry after current jobs complete.",
    "cleanup-failure": "A tool process could not be stopped or kept its output open; no result is usable. Inspect the installed executable before approving again.",
    "unsupported-platform": "External execution currently requires Linux process-group supervision; static analysis remains available.",
}


def described(result: ProcessResult) -> ProcessResult:
    """Attach the sanitized user-facing message; never derived from output."""
    return replace(result, message=RESULT_MESSAGES[result.status])


class ToolError(Exception):
    """A sanitized, actionable boundary failure (never child output)."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def bounded_read(path, limit):
    """Check the bytes actually read, not just a racy stat size."""
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ToolError(
            "invalid-config",
            "Selected config/rules exceed the content budget.",
        )
    return data


def safe_route(path, roots):
    """Reject every repository-controlled route, not just the final target.

    Walks components as the kernel does, so `..` applies to a symlink's target
    rather than to its spelling. Anything unreadable counts as unsafe.
    """
    path = Path(path)
    if not path.is_absolute():
        return False

    def inside(candidate):
        return any(candidate.is_relative_to(root) for root in roots)

    current = Path(path.anchor)
    pending = list(reversed(path.parts[1:]))
    hops = 0
    while pending:
        part = pending.pop()
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if inside(candidate):
            return False
        # Not Path.is_symlink: from Python 3.14 it reports every error as False,
        # which would make a component we cannot inspect look like a safe one.
        try:
            if not stat.S_ISLNK(os.lstat(candidate).st_mode):
                current = candidate
                continue
            target = Path(os.readlink(candidate))
        except (FileNotFoundError, NotADirectoryError):
            current = candidate
            continue
        except OSError:
            return False
        hops += 1
        if hops > 40:
            return False
        if target.is_absolute():
            current = Path(target.anchor)
            pending.extend(reversed(target.parts[1:]))
        else:
            pending.extend(reversed(target.parts))
    return not inside(current)


def rule_files(root, budget):
    """Stream directory entries so a huge directory cannot allocate first."""
    pending = [root]
    files = []
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > budget:
                    raise ToolError(
                        "invalid-config", "Rule trees exceed 1024 entries."
                    )
                if entry.is_symlink():
                    raise ToolError(
                        "invalid-config",
                        "Symlinks inside rule trees are not supported.",
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(Path(entry.path))
                else:
                    raise ToolError(
                        "invalid-config", "Rule files must be regular files."
                    )
    return sorted(files), count


@dataclass(frozen=True)
class Selection:
    """Resolved permission subject; configuration is content-pinned."""

    name: str
    context: ProjectContext
    executable: Path
    explicit: bool
    workspace_executable: bool
    config: Path | None
    rules: tuple[Path, ...]
    identity: str

    def public(self):
        """Only paths/identity, never configuration contents."""
        return {
            "executable": str(self.executable),
            "canonicalExecutable": str(self.executable.resolve()),
            "executableSource": (
                "explicit" if self.explicit else "installed-PATH"
            ),
            "workspaceExecutable": self.workspace_executable,
            "config": str(self.config) if self.config else None,
            "ruleDirs": [str(p) for p in self.rules],
            "projectRoot": str(self.context.project_root),
            "activeProfile": self.context.active_profile,
        }


class ToolService:
    """Session-local selection and permission owner, shared by all providers."""

    def __init__(
        self,
        context: Callable[[str], ProjectContext | None],
        roots: Callable[[], list[Path | str]],
    ):
        self.context = context
        self.roots = roots
        self.settings: list[dict] = []
        self.configuration_error: str | None = None
        self.tokens: dict[str, Selection] = {}
        self.grants: set[str] = set()
        self.epoch = 0
        self.runner = ProcessRunner()
        self.jobs: set[asyncio.Task] = set()
        self._listeners: list[Callable[[], None]] = []

    def on_invalidate(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a synchronous provider hook to clear tool-owned results.

        Returns an unsubscribe function. Called after permission is removed;
        never clear built-in diagnostics here. At most 16 provider listeners.
        """
        if len(self._listeners) >= 16:
            raise ValueError("Too many tool invalidation listeners")
        self._listeners.append(callback)

        def unsubscribe():
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def _notify(self):
        for callback in tuple(self._listeners):
            try:
                callback()
            except (
                Exception
            ):  # A consumer must not interrupt trust revocation.
                logging.getLogger(__name__).error(
                    "Tool invalidation listener failed (details omitted)"
                )

    def invalidate(self) -> None:
        """Revoke first, then cancel jobs. No pending job may start afterwards."""
        self.epoch += 1
        self.grants.clear()
        for job in self.jobs:
            job.cancel()
        self._notify()

    async def close(self) -> None:
        """Revoke and await bounded cleanup on server shutdown."""
        self.invalidate()
        await asyncio.gather(*self.jobs, return_exceptions=True)

    def configure(self, entries):
        """Replace settings, fail closed. Settings cannot contain trust grants."""
        self.invalidate()
        self.tokens.clear()
        self.settings = []
        self.configuration_error = "Invalid salt.tools settings; correct the settings snapshot. Workspace settings cannot grant execution trust."
        if not isinstance(entries, list) or len(entries) > 64:
            return ["salt.tools: expected at most 64 tool selections"]
        allowed = {
            "projectRoot",
            "profile",
            "name",
            "executable",
            "config",
            "ruleDirs",
            "allowSharedConfig",
        }
        for entry in entries:
            if not isinstance(entry, dict) or entry.keys() - allowed:
                return [
                    "salt.tools: invalid keys; settings cannot grant execution trust"
                ]
            if entry.get("name") not in ("salt-lint", "yamlfmt"):
                return ["salt.tools.name: expected salt-lint or yamlfmt"]
            if (
                not isinstance(entry.get("projectRoot"), str)
                or not Path(entry["projectRoot"]).is_absolute()
            ):
                return ["salt.tools.projectRoot: expected an absolute path"]
            for key in ("profile", "executable", "config"):
                if key in entry and (
                    not isinstance(entry[key], str)
                    or not entry[key]
                    or len(entry[key]) > 4096
                ):
                    return [
                        f"salt.tools.{key}: expected a nonempty literal path/name"
                    ]
            if not isinstance(entry.get("allowSharedConfig", False), bool):
                return ["salt.tools.allowSharedConfig: expected boolean"]
            rules = entry.get("ruleDirs", [])
            if (
                not isinstance(rules, list)
                or len(rules) > 32
                or any(not isinstance(p, str) or not p for p in rules)
            ):
                return ["salt.tools.ruleDirs: expected at most 32 paths"]
        self.settings = entries
        self.configuration_error = None
        return []

    def _select(self, uri, name):
        if self.configuration_error:
            raise ToolError("invalid-config", self.configuration_error)
        context = self.context(uri)
        if context is None:
            raise ToolError(
                "invalid-config", "A local project context is required."
            )
        entries = [
            e
            for e in self.settings
            if e["name"] == name
            and Path(e["projectRoot"]).resolve() == context.project_root
            and e.get("profile", "default") == context.active_profile
        ]
        if not entries:
            raise ToolError(
                "missing-tool",
                "Select one user-installed tool for this project/profile in salt.tools.",
            )
        if len(entries) > 1:
            raise ToolError(
                "invalid-config",
                "Duplicate tool selections for this project/profile; keep exactly one entry.",
            )
        entry = entries[0]
        selected = entry.get("executable")
        if selected:
            binary = Path(selected)
            if not binary.is_absolute():
                raise ToolError(
                    "invalid-config",
                    "Explicit executable paths must be absolute.",
                )
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise ToolError(
                    "missing-tool",
                    "The selected executable is missing or not executable; no fallback was attempted.",
                )
        else:
            binary = self._discover(name, context)
        identity = hashlib.sha256()
        stat = binary.stat()
        identity.update(
            repr(
                (
                    str(binary),
                    str(binary.resolve(strict=True)),
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            ).encode()
        )
        config = None
        config_mapping = {}
        rules = []
        rule_bytes = 0
        rule_entries = 0
        for value, is_rule in [(entry.get("config"), False)] + [
            (r, True) for r in entry.get("ruleDirs", [])
        ]:
            if value is None:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = context.project_root / path
            path = path.resolve(strict=True)
            if not path.is_relative_to(context.project_root) and not entry.get(
                "allowSharedConfig", False
            ):
                raise ToolError(
                    "invalid-config",
                    "Config/rules escape the project; explicit allowSharedConfig is required, followed by approval.",
                )
            identity.update(str(path).encode())
            if is_rule:
                if not path.is_dir():
                    raise ToolError(
                        "invalid-config",
                        "Selected rule directory is not a directory.",
                    )
                rules.append(path)
                # Deliberately no imports; bounded content identity covers later edits.
                files, count = rule_files(path, 1024 - rule_entries)
                rule_entries += count
                for file in files:
                    content = bounded_read(file, 4 * 1024 * 1024 - rule_bytes)
                    rule_bytes += len(content)
                    identity.update(str(file).encode())
                    identity.update(content)
            else:
                if not path.is_file() or path.stat().st_size > 65536:
                    raise ToolError(
                        "invalid-config",
                        "Selected config must be a regular file of at most 64 KiB.",
                    )
                content = bounded_read(path, 65536)
                try:
                    parsed = yaml.safe_load(content)
                    if parsed is not None and not isinstance(parsed, dict):
                        raise ValueError
                except (yaml.YAMLError, ValueError, RecursionError):
                    raise ToolError(
                        "invalid-config",
                        "Selected config must be a valid YAML mapping.",
                    ) from None
                identity.update(content)
                config = path
                config_mapping = parsed or {}
        if name == "salt-lint" and config:
            declared = config_mapping.get("rulesdir", [])
            if not isinstance(declared, list) or any(
                not isinstance(p, str) for p in declared
            ):
                raise ToolError(
                    "invalid-config",
                    "salt-lint rulesdir must be an explicit list of paths.",
                )
            native = {
                (context.project_root / p).resolve(strict=True)
                for p in declared
            }
            if native != set(rules):
                raise ToolError(
                    "invalid-config",
                    "Selected config rulesdir must exactly match the separately selected ruleDirs (relative to project root).",
                )
        elif rules and name == "yamlfmt":
            raise ToolError(
                "invalid-config",
                "yamlfmt does not support custom rule directories.",
            )
        return Selection(
            name,
            context,
            binary,
            bool(selected),
            not safe_route(
                binary,
                [
                    context.project_root,
                    *(Path(p).resolve() for p in self.roots()),
                ],
            ),
            config,
            tuple(rules),
            identity.hexdigest(),
        )

    def _discover(self, name, context):
        roots = [
            context.project_root,
            *(Path(p).resolve() for p in self.roots()),
        ]
        for component in os.environ.get("PATH", "").split(os.pathsep):
            directory = Path(component)
            if not component or not directory.is_absolute():
                continue
            candidate = directory / name
            try:
                if not safe_route(candidate, roots):
                    continue
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate
            except (OSError, RuntimeError):
                continue
        raise ToolError(
            "missing-tool",
            "No safe user-installed executable found on absolute non-project PATH entries.",
        )

    def inspect(self, uri: str, name: str) -> dict:
        """Select without executing, returning an explicit approval subject."""
        try:
            selection = self._select(uri, name)
            revoked = False
            for old_token, old in list(self.tokens.items()):
                if (
                    old.name == name
                    and old.context.project_root
                    == selection.context.project_root
                    and old != selection
                ):
                    self.tokens.pop(old_token)
                    revoked |= old_token in self.grants
                    self.grants.discard(old_token)
            if revoked:
                self._notify()
            token = next(
                (t for t, s in self.tokens.items() if s == selection), None
            )
            if token is None:
                if len(self.tokens) >= 128:
                    self.tokens.clear()
                    self.grants.clear()
                    self._notify()
                token = secrets.token_urlsafe(24)
                self.tokens[token] = selection
            return {
                "status": "ready" if token in self.grants else "denied-trust",
                "message": "Explicit approval of executable, config and custom rules is required.",
                "token": token,
                "selection": selection.public(),
            }
        except ToolError as error:
            self._forget_failed_selection(uri, name)
            return {"status": error.status, "message": str(error)}
        except (OSError, RuntimeError, ValueError):
            self._forget_failed_selection(uri, name)
            return {
                "status": "invalid-config",
                "message": "Selected paths cannot be read or resolved; check permissions and symlinks.",
            }

    def _forget_failed_selection(self, uri, name):
        context = self.context(uri)
        revoked = False
        for token, old in list(self.tokens.items()):
            if old.name == name and (
                context is None
                or old.context.project_root == context.project_root
            ):
                self.tokens.pop(token)
                revoked |= token in self.grants
                self.grants.discard(token)
        if revoked:
            self._notify()

    def authorize(self, uri: str, name: str, token: str, action: str) -> dict:
        """An editor user gesture, not a workspace setting or persisted boolean."""
        if action == "revoke":
            self.invalidate()
            return {
                "status": "denied-trust",
                "message": "All external-tool grants revoked; running work is being stopped.",
            }
        current = self.inspect(uri, name)
        if action != "allow" or current.get("token") != token:
            return {
                "status": "denied-trust",
                "message": "Selection changed; inspect and explicitly approve the current selection.",
            }
        self.grants.add(token)
        return self.inspect(uri, name)

    async def run(
        self,
        uri: str,
        name: str,
        invocation: Callable[[Selection, Path], list[str]],
        *,
        version: object,
        current_version: Callable[[], object],
        data: bytes = b"",
        timeout: float = 5.0,
        output_limit: int = 262144,
    ) -> ProcessResult:
        """Provider seam. invocation(selection, controlled_config) returns argv.

        Only server adapters supply invocation, never client-provided arguments.
        Adapter MUST disable native config discovery and assert compatibility;
        selected rules/config have already been separately authorized here.
        """
        status = self.inspect(uri, name)
        token = status.get("token")
        if status["status"] != "ready":
            return ProcessResult(status["status"], message=status["message"])
        assert isinstance(token, str)
        selection = self.tokens[token]
        epoch = self.epoch

        def current():
            return (
                epoch == self.epoch
                and token in self.grants
                and current_version() == version
            )

        def valid():
            """Also rereads the selected files, so it is not for every tick."""
            return current() and self.inspect(uri, name).get("token") == token

        # A controlled empty config is preferable to any ancestor/HOME fallback.
        with tempfile.TemporaryDirectory(prefix="salt-lsp-tool-") as directory:
            default = Path(directory) / "config.yaml"
            default.write_text("{}\n", encoding="utf-8")
            config = selection.config or default
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("PYTHON") and key != "VIRTUAL_ENV"
            }
            env["PYTHONSAFEPATH"] = "1"
            env["PYTHONNOUSERSITE"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["HOME"] = directory
            env["XDG_CONFIG_HOME"] = directory
            roots = [
                selection.context.project_root,
                *(Path(p).resolve() for p in self.roots()),
            ]
            env["PATH"] = os.pathsep.join(
                p
                for p in os.environ.get("PATH", "").split(os.pathsep)
                if p and safe_route(Path(p), roots)
            )
            job = asyncio.create_task(
                self.runner.run(
                    selection.executable,
                    invocation(selection, config),
                    cwd=selection.context.project_root,
                    env=env,
                    data=data,
                    valid=valid,
                    timeout=timeout,
                    output_limit=output_limit,
                )
            )
            self.jobs.add(job)

            async def monitor():
                ticks = 0
                while not job.done():
                    await asyncio.sleep(0.05)
                    ticks += 1
                    if not (valid() if ticks % 10 == 0 else current()):
                        job.cancel()
                        return

            watcher = asyncio.create_task(monitor())
            try:
                result = await job
            except asyncio.CancelledError:
                # Revocation or staleness is a result. A cancelled caller is not:
                # swallowing that would defeat its timeout or supersession.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                return described(ProcessResult("cancelled"))
            finally:
                self.jobs.discard(job)
                watcher.cancel()
            return described(result if valid() else ProcessResult("stale"))

    async def probe(self, uri, name):
        """Version probes are execution, behind exactly the same gate as runs."""
        status = self.inspect(uri, name)
        if status["status"] != "ready":
            return status
        if name != "yamlfmt":
            return {
                "status": "unsupported-capability",
                "message": "salt-lint requires the explicitly selected patched adapter's capability handshake (#17); no stock probe was executed.",
            }
        result = await self.run(
            uri,
            name,
            lambda selection, config: [
                "-conf",
                str(config),
                "-no_global_conf",
                "-version",
            ],
            version=None,
            current_version=lambda: None,
        )
        if result.status != "clean":
            return {"status": result.status, "message": result.message}
        if not result.stdout.startswith(b"yamlfmt 0.21.0 "):
            return {
                "status": "unsupported-capability",
                "message": "Expected yamlfmt 0.21.0; select the supported version explicitly.",
            }
        return {
            "status": "clean",
            "message": "yamlfmt 0.21.0 compatibility probe completed.",
        }
