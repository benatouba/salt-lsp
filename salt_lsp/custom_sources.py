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
        functions, values = _bindings(tree.body)
        modules = _module_names(path, functions, values)
        restriction = values.get(
            "__load__", values.get("__all__", "unrestricted")
        )
        aliases = values.get("__func_alias__", {})
        aliases = aliases if isinstance(aliases, dict) else {}
        return [
            {
                "kind": kind,
                "module": module,
                "function": aliases.get(name, name),
                "name": f"{module}.{aliases.get(name, name)}",
                "provider": str(path),
                "line": function.lineno,
                "documentation": ast.get_docstring(function) or "",
                "arguments": _arguments(function),
                "authoring": True,
                "alias_of": function.name if function.name != name else None,
                "uri": path.as_uri(),
                "source_name": function.name,
                "source_range": _definition_range(function, source),
            }
            for name, candidates in functions.items()
            for function in candidates
            for module in modules
            if not name.startswith("_")
            and (
                restriction is None
                or restriction == "unrestricted"
                or isinstance(restriction, (list, tuple))
                and name in restriction
            )
            and isinstance(aliases.get(name, name), str)
        ]
    except (SyntaxError, ValueError, RecursionError):
        # Invalid live source is unknown, never replaced with last-good/disk metadata.
        return []


def _literal(node):
    """A deliberately tiny literal grammar; never invoke source callables."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        pairs = [
            (_literal(key), _literal(value))
            for key, value in zip(node.keys, node.values)
        ]
        return (
            dict(pairs) if all(isinstance(k, str) for k, _ in pairs) else None
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and not node.args
    ):
        return {kw.arg: _literal(kw.value) for kw in node.keywords if kw.arg}
    return None


def _bindings(nodes):
    functions, values = {}, {}
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = [node]
        elif isinstance(node, ast.Assign):
            value = _literal(node.value)
            bound = (
                functions.get(node.value.id, [])
                if isinstance(node.value, ast.Name)
                else []
            )
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
                    functions[target.id] = list(bound)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    functions.pop(target.id, None)
                    values[target.id] = None
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            suites = [
                node.body,
                getattr(node, "orelse", []),
                getattr(node, "finalbody", []),
            ]
            suites.extend(
                handler.body for handler in getattr(node, "handlers", [])
            )
            for suite in suites:
                conditional, metadata = _bindings(suite)
                for name, candidates in conditional.items():
                    functions.setdefault(name, []).extend(candidates)
                # A conditional mutation invalidates certainty, not other names.
                for name in metadata:
                    values[name] = None
    return functions, values


def _returns(nodes):
    for node in nodes:
        if isinstance(node, ast.Return):
            yield node.value
        elif not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            yield from _returns(list(ast.iter_child_nodes(node)))


def _module_names(path, functions, values):
    hooks = functions.get("__virtual__", [])
    names = set()
    declared = values.get("__virtualname__")
    for hook in hooks:
        if isinstance(declared, str):
            names.add(declared)
        for returned in _returns(hook.body):
            if isinstance(returned, ast.Constant) and isinstance(
                returned.value, str
            ):
                names.add(returned.value)
    if not names:
        names.add(path.stem)
    aliases = values.get("__virtual_aliases__", [])
    if isinstance(aliases, (list, tuple)):
        names.update(name for name in aliases if isinstance(name, str))
    return sorted(names)


def _definition_range(function, source):
    line = source.splitlines()[function.lineno - 1]
    start = len(line.encode("utf-8")[: function.col_offset].decode("utf-8"))
    start = line.index(
        function.name,
        start + (10 if isinstance(function, ast.AsyncFunctionDef) else 4),
    )
    return (function.lineno - 1, start, start + len(function.name))


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
                if ordered:
                    sources.append(
                        {
                            "kind": kind,
                            "source_only": True,
                            "source_identity": path.stem,
                        }
                    )
                sources.extend(_extract(path, source, kind))
            except (OSError, UnicodeError):
                continue
    return sources
