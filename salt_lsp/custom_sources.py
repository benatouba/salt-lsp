"""Bounded, non-importing source declarations with authoritative live overlays."""

import ast
import os
from pathlib import Path

from salt_lsp.project import file_path

MAX_SOURCE = 1024 * 1024
MAX_FILES = 2048


def _expression(node):
    return ast.unparse(node) if node is not None else None


def _arguments(function):
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    values = [
        (
            arg,
            (
                "positional_only"
                if index < len(args.posonlyargs)
                else "positional_or_keyword"
            ),
            default,
        )
        for index, (arg, default) in enumerate(zip(positional, defaults))
    ]
    if args.vararg:
        values.append((args.vararg, "var_positional", None))
    values.extend(
        (arg, "keyword_only", default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults)
    )
    if args.kwarg:
        values.append((args.kwarg, "var_keyword", None))
    return [
        {
            "name": arg.arg,
            "kind": kind,
            "default": _expression(default),
            "has_default": default is not None,
            "annotation": _expression(arg.annotation),
        }
        for arg, kind, default in values
    ]


def _extract(path, source, kind):
    if len(source) > MAX_SOURCE:
        return []
    try:
        tree = ast.parse(source)
        functions = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[node.name] = node
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        functions.pop(target.id, None)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        functions.pop(target.id, None)
        return [
            {
                "kind": kind,
                "module": path.stem,
                "function": name,
                "name": f"{path.stem}.{name}",
                "provider": str(path),
                "line": function.lineno,
                "documentation": ast.get_docstring(function) or "",
                "arguments": _arguments(function),
                "authoring": True,
                "alias_of": None,
                "uri": path.as_uri(),
                "source_name": function.name,
            }
            for name, function in functions.items()
            if not name.startswith("_")
        ]
    except (SyntaxError, ValueError, RecursionError):
        # Invalid live source is unknown, never replaced with last-good/disk metadata.
        return []


def discover(workspace, context):
    """Inspect on request: deletion/disk changes need no stale-cache invalidation.

    State-root relative paths have configured first-root precedence. Additional
    moduleDirs are execution sources, with no assumed priority over one another.
    Only immediate .py sources are supported (not arbitrary package helpers).
    """
    sources = []
    visited = 0
    total = 0
    seen = set()
    overlays = {}
    for uri, document in workspace.text_documents.items():
        try:
            overlays[file_path(uri)] = document.source
        except ValueError:
            continue
    directories = [
        (root / folder, kind, True)
        for root in context.state_roots
        for folder, kind in (("_states", "state"), ("_modules", "execution"))
    ] + [(root, "execution", False) for root in context.module_dirs]
    for directory, kind, ordered in directories:
        candidates = set()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_FILES:
                        break
                    if entry.name.endswith(".py") and entry.is_file():
                        candidates.add(Path(entry.path))
        except OSError:
            pass
        candidates.update(
            p for p in overlays if p.parent == directory and p.suffix == ".py"
        )
        for path in sorted(candidates):
            if visited > MAX_FILES:
                return sources
            if path.name.startswith("_") or not path.resolve().is_relative_to(
                directory.resolve()
            ):
                continue
            selected = workspace.project_context(path.as_uri())
            if (
                selected is None
                or selected.project_root != context.project_root
                or selected.active_profile != context.active_profile
            ):
                continue
            key = (kind, path.name)
            if ordered and key in seen:
                continue
            if ordered:
                seen.add(key)
            try:
                source = overlays.get(path.resolve())
                if source is None:
                    with path.open(encoding="utf-8") as stream:
                        source = stream.read(MAX_SOURCE + 1)
                total += len(source)
                if total > 16 * MAX_SOURCE:
                    return sources
                sources.extend(_extract(path, source, kind))
            except (OSError, UnicodeError):
                continue
    return sources
