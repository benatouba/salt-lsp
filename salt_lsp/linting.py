"""Trailing-edge linting through the explicitly approved text/path adapter."""

import asyncio
import json
from pathlib import Path

from lsprotocol import types

from salt_lsp.project import file_path


CAPABILITIES = {
    "protocol": 1,
    "salt_lint": "0.9.2",
    "python": [3, 12],
    "pyyaml": "6.0.3",
    "pathspec": "1.1.1",
    "stdin_filename": True,
}


def findings(stdout, document):
    """Validate the entire response before publishing any finding."""
    items = json.loads(stdout)
    if not isinstance(items, list) or len(items) > 2048:
        raise ValueError
    result = []
    lines = document.lines or [""]
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("filename"), str)
            or Path(item["filename"]).resolve() != file_path(document.uri)
            or type(item.get("linenumber")) is not int
            or not 1 <= item["linenumber"] <= len(lines)
            or not isinstance(item.get("message"), str)
            or len(item["message"]) > 8192
            or not isinstance(item.get("id"), (str, int))
            or item.get("severity")
            not in ("INFO", "LOW", "MEDIUM", "HIGH", "VERY_HIGH")
        ):
            raise ValueError
        line = item["linenumber"] - 1
        span = document.position_codec.range_to_client_units(
            lines,
            types.Range(
                types.Position(line, 0),
                types.Position(line, len(lines[line].rstrip("\r\n"))),
            ),
        )
        result.append(
            types.Diagnostic(
                range=span,
                source="salt-lint",
                code=str(item["id"]),
                message=item["message"],
                severity=(
                    types.DiagnosticSeverity.Information
                    if item["severity"] == "INFO"
                    else types.DiagnosticSeverity.Warning
                ),
            )
        )
    return result


class LintService:
    """Own cancellation, bounded scheduling, status and only lint diagnostics."""

    def __init__(self, server):
        self.server = server
        self.jobs = {}
        self.status = {}
        server.tools.on_invalidate(self.invalidate)

    def invalidate(self):
        for job in self.jobs.values():
            job.cancel()
        self.jobs.clear()
        for uri in tuple(self.status):
            self.server.diagnostics.set(uri, "salt-lint", [])
        self.status.clear()
        self.refresh()

    def refresh(self, changed_uri=None):
        workspace = self.server.workspace
        targets = (
            [changed_uri]
            if changed_uri
            else list(dict.fromkeys([*self.jobs, *workspace.text_documents]))[
                :128
            ]
        )
        for uri in targets:
            previous = self.jobs.pop(uri, None)
            if previous:
                previous.cancel()
            document = workspace.text_documents.get(uri)
            context = workspace.project_context(uri)
            if uri in self.status or (
                document and context and context.file_role == "state"
            ):
                self.server.diagnostics.set(uri, "salt-lint", [])
            if not document or not context or context.file_role != "state":
                self.status.pop(uri, None)
                continue
            state = self.server.tools.inspect(uri, "salt-lint")
            self.status[uri] = {
                "status": state["status"],
                "message": state["message"],
            }
            if state["status"] == "ready" and len(self.jobs) < 128:
                self.status[uri] = {
                    "status": "pending",
                    "message": "Waiting for one second without edits.",
                }
                self.jobs[uri] = asyncio.create_task(self.lint(uri))

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        self.jobs.clear()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def lint(self, uri):
        try:
            await asyncio.sleep(1)
            await self.run(uri)
        except asyncio.CancelledError:
            pass
        finally:
            if self.jobs.get(uri) is asyncio.current_task():
                self.jobs.pop(uri, None)

    async def run(self, uri):
        workspace = self.server.workspace
        document = workspace.text_documents.get(uri)
        context = workspace.project_context(uri)
        if not document or not context or context.file_role != "state":
            return
        source = document.source.encode("utf-8")
        snapshot = (
            workspace.document_stamp(uri),
            context,
            self.server.tools.epoch,
        )

        def current():
            return (
                workspace.document_stamp(uri),
                workspace.project_context(uri),
                self.server.tools.epoch,
            )

        if len(source) > 1024 * 1024:
            self.status[uri] = {
                "status": "input-limit",
                "message": "Lint input exceeds 1 MiB.",
            }
            return
        probe = await self.server.tools.probe(uri, "salt-lint")
        if current() != snapshot:
            return
        if probe["status"] != "clean":
            self.status[uri] = probe
            return

        def invocation(selection, config):
            args = [
                "-c",
                str(config),
                "--json",
                "--stdin-filename",
                str(file_path(uri)),
            ]
            if selection.config is None:
                for rule in selection.rules:
                    args.extend(["-r", str(rule)])
            return args

        result = await self.server.tools.run(
            uri,
            "salt-lint",
            invocation,
            version=snapshot,
            current_version=current,
            data=source,
        )
        if current() != snapshot:
            return
        if result.status not in (
            "clean",
            "tool-failure",
        ) or result.returncode not in (0, 2):
            self.status[uri] = {
                "status": result.status,
                "message": result.message,
            }
            return
        try:
            diagnostics = findings(result.stdout, document)
            if bool(diagnostics) != (result.returncode == 2):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, OSError, RecursionError):
            self.status[uri] = {
                "status": "invalid-result",
                "message": "salt-lint returned malformed output or a configuration/execution failure; no findings were accepted.",
            }
            return
        if current() != snapshot:
            return
        self.status[uri] = {
            "status": "findings" if diagnostics else "clean",
            "message": "salt-lint completed on the current unsaved buffer.",
        }
        self.server.diagnostics.set(uri, "salt-lint", diagnostics)
