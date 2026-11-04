"""Bounded, non-rendering source lookup with live-buffer authority.

Every request owns its snapshot. Disk sources never masquerade as open buffers,
so dependency removal, disk edits, profile changes and close need no stale cache.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from lsprotocol import types
from pygls.workspace import TextDocument

from salt_lsp.project import file_path
from salt_lsp.state_calls import _project_template

MAX_BYTES = 1024 * 1024
MAX_FILES = 2048
MAX_DEPENDENCIES = 128
_JINJA = re.compile(
    r"\{%[-+]?\s*(?:include|import|from)\s+(['\"])([^'\"\r\n]*)(?:\1|$)"
)


@dataclass(frozen=True)
class Reference:
    """Literal source spelling and original code-point span."""

    name: str
    line: int
    start: int
    end: int
    kind: str = "sls"
    environment: str | None = None


def references(source, top=False):
    """Recover block-list includes and quoted Jinja references, even mid-edit."""
    projected = _project_template(source).splitlines()
    include = False
    top_environment = None
    for line, text in enumerate(source.splitlines()):
        for match in _JINJA.finditer(text):
            tail = text[match.end() :].lstrip()
            if not tail or tail.startswith(
                (
                    "%}",
                    "-%}",
                    "+%}",
                    "import ",
                    "as ",
                    "ignore missing",
                    "with context",
                    "without context",
                )
            ):
                yield Reference(
                    match[2], line, *match.span(2), kind="template"
                )
        text = projected[line] if line < len(projected) else ""
        if re.match(r"^include\s*:\s*(?:#.*)?$", text):
            include = True
            continue
        if top and (match := re.match(r"^([^\s:#]+)\s*:\s*$", text)):
            top_environment = match[1]
        elif text and not text[0].isspace() and not text.startswith("#"):
            include = False
        if not include and not (top and top_environment):
            continue
        match = re.match(r"\s+-\s*(.*)", text)
        if not match:
            continue
        value = match[1]
        start = match.start(1)
        environment = top_environment if top else None
        if env := re.match(r"([\w-]+):\s*(.*)", value):
            environment = env[1]
            start += env.start(2)
            value = env[2]
        if value.startswith(("'", '"')):
            quote = value[0]
            start += 1
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = value.split("#", 1)[0].rstrip()
        if not any(c in value for c in "{}[]\x00"):
            yield Reference(
                value, line, start, start + len(value), environment=environment
            )


class SourceView:
    """A request-local source boundary, shared by includes and requisites."""

    def __init__(self, workspace, context):
        self.workspace = workspace
        self.context = context
        self.roots = (
            context.pillar_roots
            if context.file_role.startswith("pillar")
            else context.state_roots
        )
        self.documents = {}
        self.bytes = 0
        self.overlays = {
            file_path(uri): doc
            for uri, doc in workspace.text_documents.items()
        }

    def permitted(self, path, roots=None):
        """Canonical containment plus project ownership; never follow escapes."""
        try:
            canonical = path.resolve()
            selected = self.workspace.project_context(canonical.as_uri())
            return (
                any(
                    canonical.is_relative_to(root)
                    for root in (self.roots if roots is None else roots)
                )
                and selected is not None
                and selected.project_root == self.context.project_root
                and selected.active_profile == self.context.active_profile
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def document(self, path):
        """Read bounded disk text or the exact current editor source."""
        path = path.resolve()
        if path in self.documents:
            return self.documents[path]
        if (
            len(self.documents) >= MAX_DEPENDENCIES
            or self.bytes >= 16 * MAX_BYTES
        ):
            return None
        try:
            if path in self.overlays:
                document = self.overlays[path]
                source = document.source
            else:
                if path.stat().st_size > MAX_BYTES:
                    return None
                with path.open(encoding="utf-8") as stream:
                    source = stream.read(MAX_BYTES + 1)
                document = TextDocument(
                    path.as_uri(),
                    source,
                    position_codec=self.workspace.position_codec,
                )
            size = len(source.encode("utf-8"))
            if size > MAX_BYTES:
                return None
            self.bytes += size
            self.documents[path] = document
            return document
        except (OSError, UnicodeError, ValueError):
            return None

    def _relative_name(self, origin, name):
        if not name.startswith("."):
            return name
        count = len(name) - len(name.lstrip("."))
        for root in self.roots:
            if origin.is_relative_to(root):
                parts = list(origin.relative_to(root).parent.parts)
                if count > len(parts):
                    return None
                return ".".join(
                    parts[: len(parts) - count + 1] + [name[count:]]
                )
        return None

    def resolve(self, origin, reference):
        """Ordered roots win; file and directory forms remain ambiguous."""
        if reference.environment not in (None, self.context.environment):
            return []
        name = reference.name
        if reference.kind == "template":
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or any(p in ("", ".", "..") for p in name.split("/"))
            ):
                return []
            for root in self.roots:
                candidate = root / name
                if self.permitted(candidate) and self.document(candidate):
                    return [candidate.resolve()]
            return []
        name = self._relative_name(origin, name)
        if (
            not name
            or any(c in name for c in "/\\\x00")
            or any(not p for p in name.split("."))
        ):
            return []
        relative = Path(*name.split("."))
        for root in self.roots:
            found = [
                p.resolve()
                for p in (
                    root / relative.with_suffix(".sls"),
                    root / relative / "init.sls",
                )
                if self.permitted(p) and self.document(p)
            ]
            if found:
                return list(dict.fromkeys(found))
        return []

    def closure(self, origin):
        """Iterative bounded include closure, with cycle and self-include guards."""
        pending, seen = [origin], set()
        links = 0
        while pending and len(seen) < MAX_DEPENDENCIES:
            path = pending.pop(0).resolve()
            if path in seen or (path != origin and not self.permitted(path)):
                continue
            seen.add(path)
            document = self.document(path)
            if document is None:
                continue
            yield path, document
            for reference in references(document.source):
                if reference.kind == "sls":
                    links += 1
                    if links > MAX_FILES:
                        return
                    pending.extend(
                        p
                        for p in self.resolve(path, reference)
                        if p not in seen
                    )

    def names(self, template=False, exclude=None):
        """Bounded non-symlink recursive scan, including unsaved new files."""
        candidates = set(self.overlays)
        visited = 0
        for root in self.roots:
            pending = [root]
            while pending and visited < MAX_FILES:
                directory = pending.pop()
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            visited += 1
                            if visited >= MAX_FILES:
                                break
                            if entry.name.startswith((".", "_")):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file():
                                candidates.add(Path(entry.path))
                except OSError:
                    continue
        names = set()
        for candidate in candidates:
            if candidate == exclude:
                continue
            if not self.permitted(candidate):
                continue
            for root in self.roots:
                if not candidate.is_relative_to(root):
                    continue
                relative = candidate.relative_to(root)
                if template:
                    names.add(relative.as_posix())
                elif relative.suffix == ".sls" and relative.name != "top.sls":
                    parts = list(relative.with_suffix("").parts)
                    if parts[-1] == "init":
                        parts.pop()
                    if parts and all(
                        part and "." not in part for part in parts
                    ):
                        names.add(".".join(parts))
        return sorted(names)


def at_reference(workspace, document, position, context, completion=False):
    """Standard completion/definition for a source reference under the cursor."""
    point = document.position_codec.position_from_client_units(
        document.lines, position
    )
    view = SourceView(workspace, context)
    origin = file_path(document.uri)
    for reference in references(
        document.source, context.file_role.endswith("-top")
    ):
        if (
            reference.line != point.line
            or not reference.start <= point.character <= reference.end
        ):
            continue
        if not completion:
            return [
                types.Location(
                    path.as_uri(),
                    types.Range(types.Position(0, 0), types.Position(0, 0)),
                )
                for path in view.resolve(origin, reference)
            ] or None
        prefix = reference.name[: point.character - reference.start]
        names = view.names(reference.kind == "template", exclude=origin)
        if prefix.startswith(".") and reference.kind == "sls":
            relative_prefix = view._relative_name(origin, prefix)
            if relative_prefix is None:
                names = []
            else:
                base = relative_prefix[
                    : len(relative_prefix) - len(prefix.lstrip("."))
                ]
                dots = prefix[: len(prefix) - len(prefix.lstrip("."))]
                names = [
                    dots + name[len(base) :]
                    for name in names
                    if name.startswith(base)
                ]
        span = document.position_codec.range_to_client_units(
            document.lines,
            types.Range(
                types.Position(reference.line, reference.start),
                types.Position(reference.line, reference.end),
            ),
        )
        return types.CompletionList(
            is_incomplete=False,
            items=[
                types.CompletionItem(
                    label=name, text_edit=types.TextEdit(span, name)
                )
                for name in names
                if name.startswith(prefix)
            ],
        )
    return None
