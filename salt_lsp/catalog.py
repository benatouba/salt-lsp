"""Offline source-known API metadata and conservative state-key completion.

Catalog selection is per request, never a mutable process-wide active version.
The data model keys providers separately and reserves ``kind`` for future APIs.
"""

from functools import lru_cache
from contextlib import contextmanager
from contextvars import ContextVar
import gzip
from importlib.resources import files
import json
import re
from typing import Any

from lsprotocol import types
from pygls.workspace import TextDocument


_PROJECT_ENTRIES: ContextVar[tuple[dict, ...]] = ContextVar(
    "project_entries", default=()
)


@contextmanager
def project_catalog(entries):
    """Request-local source overlay; never change the cached packaged catalogs."""
    token = _PROJECT_ENTRIES.set(tuple(entries))
    try:
        yield
    finally:
        _PROJECT_ENTRIES.reset(token)


def load_catalog(version: str, kind: str = "state") -> dict[str, Any]:
    """Select the packaged API plus the current request's project declarations."""
    catalog = _packaged_catalog(version, kind)
    entries = [e for e in _PROJECT_ENTRIES.get() if e["kind"] == kind]
    return (
        {**catalog, "entries": [*catalog["entries"], *entries]}
        if entries
        else catalog
    )


@lru_cache(maxsize=6)
def _packaged_catalog(version: str, kind: str = "state") -> dict[str, Any]:
    """Read only packaged, version-specific metadata; no Salt or project imports."""
    if version not in ("3006", "3007", "3008"):
        raise ValueError(f"unsupported Salt catalog: {version}")
    if kind not in ("state", "execution"):
        raise ValueError(f"unsupported catalog kind: {kind}")
    name = "states" if kind == "state" else "modules"
    resource = files("salt_lsp").joinpath(f"catalogs/{name}-{version}.json.gz")
    return json.loads(gzip.decompress(resource.read_bytes()))


def state_key(document: TextDocument, position: types.Position):
    """Locate a partial mapping key immediately under a state ID.

    Deliberately tolerate unfinished YAML, but never guess inside parameter
    values, comments, block scalars, includes, or templates.
    """
    lines = document.source.splitlines(keepends=True)
    if position.line >= len(lines):
        return None
    point = document.position_codec.position_from_client_units(
        lines, types.Position(position.line, position.character)
    )
    line = lines[point.line].rstrip("\r\n")
    match = re.fullmatch(r"( +)([A-Za-z_][\w.]*)?(?::.*)?\s*", line)
    if not match:
        return None
    indent = len(match[1])
    token = match[2] or ""
    start = indent
    end = start + len(token)
    if not start <= point.character <= end:
        return None
    parent = None
    for previous in reversed(lines[: point.line]):
        stripped = previous.strip()
        if not stripped or stripped.startswith("#"):
            continue
        width = len(previous) - len(previous.lstrip(" "))
        if width < indent:
            parent = previous.rstrip("\r\n")
            break
    if parent is None or parent.startswith(" "):
        return None
    if not re.fullmatch(r"[^#{}\[\]\s].*:\s*(?:#.*)?", parent):
        return None
    if parent.split(":", 1)[0] in ("include", "exclude", "extend"):
        return None
    prefix = token[: point.character - start]
    if "." in prefix:
        module, partial = prefix.split(".", 1)
        start += len(module) + 1
        return module, partial, start, end
    if "." in token:
        end = indent + token.index(".")
    return None, prefix, start, end


def format_signature(entry: dict[str, Any]) -> str:
    """Display source expressions rather than pretending defaults were evaluated."""
    parts = []
    args = entry["arguments"]
    for index, arg in enumerate(args):
        kind = arg["kind"]
        if kind == "keyword_only" and (
            index == 0
            or args[index - 1]["kind"]
            not in ("keyword_only", "var_positional")
        ):
            parts.append("*")
        name = {"var_positional": "*", "var_keyword": "**"}.get(
            kind, ""
        ) + arg["name"]
        if arg["annotation"]:
            name += f": {arg['annotation']}"
        if arg["has_default"]:
            name += f"={arg['default']}"
        parts.append(name)
        if kind == "positional_only" and (
            index + 1 == len(args)
            or args[index + 1]["kind"] != "positional_only"
        ):
            parts.append("/")
    return f"{entry['name']}({', '.join(parts)})"


def complete_states(
    document: TextDocument, position: types.Position, version: str
) -> types.CompletionList | None:
    """Complete only source-known state module/function mapping keys."""
    key = state_key(document, position)
    if key is None:
        return None
    module, prefix, start, end = key
    catalog = load_catalog(version)
    release = catalog["provenance"]["release"]
    codec = document.position_codec
    lines = document.source.splitlines(keepends=True)
    edit_range = codec.range_to_client_units(
        lines,
        types.Range(
            types.Position(position.line, start),
            types.Position(position.line, end),
        ),
    )
    items = []
    if module is None:
        for name in sorted(
            {e["module"] for e in catalog["entries"] if e["authoring"]}
        ):
            if name.startswith(prefix):
                items.append(
                    types.CompletionItem(
                        label=name,
                        kind=types.CompletionItemKind.Module,
                        detail=f"Salt {release} source-known state module",
                        documentation="Catalog membership is not proof of provider availability.",
                        text_edit=types.TextEdit(
                            edit_range,
                            (
                                name
                                if lines[position.line][end : end + 1] == "."
                                else name + "."
                            ),
                        ),
                    )
                )
    else:
        for entry in catalog["entries"]:
            if (
                entry["module"] != module
                or not entry["authoring"]
                or not entry["function"].startswith(prefix)
            ):
                continue
            provider = f"{entry['provider']}:{entry['line']}"
            items.append(
                types.CompletionItem(
                    label=entry["function"],
                    kind=types.CompletionItemKind.Function,
                    detail=f"{format_signature(entry)} — Salt {release}; {provider}",
                    documentation=(
                        f"Salt {release} source-known state API ({provider}).\n"
                        "Catalog membership is not proof of provider availability. "
                        "Runtime gates/dependencies and decorators are not evaluated.\n\n"
                        + entry["documentation"]
                    ),
                    text_edit=types.TextEdit(edit_range, entry["function"]),
                    data={
                        "kind": "state",
                        "version": version,
                        "name": entry["name"],
                        "provider": entry["provider"],
                    },
                )
            )
    return types.CompletionList(is_incomplete=False, items=items)
