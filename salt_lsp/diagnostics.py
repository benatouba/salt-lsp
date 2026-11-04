"""Small, source-owned diagnostic sets; unknown runtime facts are not errors."""

import re
from pathlib import Path

import yaml
from lsprotocol import types

from salt_lsp.project import file_path
from salt_lsp.source_references import SourceView, references


def builtin(workspace, document, context):
    if (
        context is None
        or context.file_role != "state"
        or len(document.source) > 1024 * 1024
    ):
        return []
    findings = []

    def add(code, message, line, start, end, severity):
        span = document.position_codec.range_to_client_units(
            document.lines,
            types.Range(
                types.Position(line, start), types.Position(line, end)
            ),
        )
        findings.append(
            types.Diagnostic(
                range=span,
                message=message,
                severity=severity,
                source="salt-lsp",
                code=code,
            )
        )

    # Without template control flow, duplicates in one YAML mapping are certain.
    # Never merge IDs across files or branches into a pretend compiled tree.
    if not any(tag in document.source for tag in ("{{", "{%", "{#")):
        try:
            node = yaml.compose(document.source)
        except (yaml.YAMLError, RecursionError):
            node = None
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key, value in node.value:
                if (
                    not isinstance(key, yaml.ScalarNode)
                    or key.tag != "tag:yaml.org,2002:str"
                    or key.end_mark.line != key.start_mark.line
                ):
                    continue
                name = key.value
                if name in ("include", "extend", "exclude"):
                    if (
                        name == "include"
                        and not isinstance(value, yaml.SequenceNode)
                        and value.tag != "tag:yaml.org,2002:null"
                    ):
                        add(
                            "include-not-list",
                            "Salt include must be a list of SLS references.",
                            key.start_mark.line,
                            key.start_mark.column,
                            key.end_mark.column,
                            types.DiagnosticSeverity.Error,
                        )
                    continue
                if name in seen:
                    add(
                        "duplicate-id",
                        "Repeated literal state ID in this unconditional YAML mapping.",
                        key.start_mark.line,
                        key.start_mark.column,
                        key.end_mark.column,
                        types.DiagnosticSeverity.Error,
                    )
                seen.add(name)

    view = SourceView(workspace, context)
    origin = file_path(document.uri)
    for reference in list(references(document.source))[:128]:
        if (
            reference.kind != "sls"
            or reference.environment not in (None, context.environment)
            or not re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", reference.name)
        ):
            continue
        if not view.roots or view.resolve(origin, reference):
            continue
        relative = Path(*reference.name.split("."))
        absent = True
        for root in view.roots:
            for candidate in (
                root / relative.with_suffix(".sls"),
                root / relative / "init.sls",
            ):
                if (
                    not view.permitted(candidate)
                    or candidate.resolve() in view.overlays
                ):
                    absent = False
                    continue
                try:
                    root.stat()
                    candidate.stat()
                    absent = False
                except FileNotFoundError:
                    if not root.is_dir():
                        absent = False
                except OSError:
                    absent = False
        if absent:
            add(
                "unresolved-sls",
                "No literal SLS source exists in the active profile's roots; a runtime loader may still supply it.",
                reference.line,
                reference.start,
                reference.end,
                types.DiagnosticSeverity.Information,
            )
    return findings


class DiagnosticService:
    """Merge independent owners without one provider clearing another's work."""

    def __init__(self, server):
        self.server = server
        self.results = {}
        self.published = set()

    def set(self, uri, owner, findings):
        self.results.setdefault(uri, {})[owner] = findings
        self.publish(uri)

    def publish(self, uri):
        document = self.server.workspace.text_documents.get(uri)
        findings = (
            [
                finding
                for group in self.results.get(uri, {}).values()
                for finding in group
            ]
            if document
            else []
        )
        self.server.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                diagnostics=findings,
                version=document.version if document else None,
            )
        )
        self.published.add(uri)

    def refresh(self, changed_uri=None):
        workspace = self.server.workspace
        for uri in tuple(self.published - set(workspace.text_documents)):
            self.results.pop(uri, None)
            self.publish(uri)
            self.published.discard(uri)
        for uri, document in list(workspace.text_documents.items()):
            context = workspace.project_context(uri)
            if (
                context and context.file_role == "state"
            ) or uri in self.published:
                self.set(uri, "builtin", builtin(workspace, document, context))
