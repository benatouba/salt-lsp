"""Generate source-known Salt state metadata using ASTs, never Salt imports.

Input archives are data: their hashes must match the explicit lock. No extraction,
network, imports from the archive, loaders, or target project access is performed.
"""

import argparse
import ast
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile


SCHEMA = 1
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_SOURCE = 5 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def literal(node, fallback=None):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return fallback


def expression(node):
    return ast.unparse(node) if node is not None else None


def statements(body):
    """Module-scope suites only; never descend into functions or classes."""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            yield from statements(node.body)
            yield from statements(getattr(node, "orelse", []))
            yield from statements(getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                yield from statements(handler.body)


def signature(node):
    args = node.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + args.defaults
    result = []
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        result.append(
            argument(
                arg,
                (
                    "positional_only"
                    if index < len(args.posonlyargs)
                    else "positional_or_keyword"
                ),
                default,
            )
        )
    if args.vararg:
        result.append(argument(args.vararg, "var_positional", None))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        result.append(argument(arg, "keyword_only", default))
    if args.kwarg:
        result.append(argument(args.kwarg, "var_keyword", None))
    return result


def argument(arg, kind, default):
    return {
        "name": arg.arg,
        "kind": kind,
        "annotation": expression(arg.annotation),
        "has_default": default is not None,
        "default": expression(default),
    }


def read_archive(path, expected_hash):
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("archive exceeds size limit")
    if digest(path.read_bytes()) != expected_hash:
        raise ValueError(f"checksum mismatch: {path.name}")
    sources = {}
    total_size = 0
    with tarfile.open(path, "r:gz") as archive:
        for index, member in enumerate(archive):
            if index > 100000:
                raise ValueError("too many archive members")
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise ValueError("unsafe archive path")
            if len(parts) < 4 or parts[1] != "salt":
                continue
            if not parts[-1].endswith(".py"):
                continue
            if not member.isfile() or member.size > MAX_SOURCE:
                raise ValueError("state source must be a bounded regular file")
            total_size += member.size
            if total_size > MAX_ARCHIVE:
                raise ValueError("source inventory exceeds size limit")
            name = "/".join(parts[1:])
            if name in sources:
                raise ValueError("duplicate source path")
            sources[name] = archive.extractfile(member).read()
    if not sources:
        raise ValueError("no state sources")
    return sources


def extract_module(path, source, sources, kind="state"):
    tree = ast.parse(source, filename=path)
    nodes = list(statements(tree.body))
    values = {}
    assignments = {}
    definitions = {}
    imported = []
    references = {}
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = literal(node.value)
                    assignments[target.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.append(
                {
                    "line": node.lineno,
                    "expression": expression(node),
                    "classification": "not exported without a recognized local clone",
                }
            )
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and not node.level
            ):
                for alias in node.names:
                    references[alias.asname or alias.name] = (
                        node.module.replace(".", "/") + ".py",
                        alias.name,
                    )
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute) and isinstance(
                call.func.value, ast.Name
            ):
                export_name = call.func.value.id
                if (
                    export_name in ("__load__", "__all__")
                    and isinstance(values.get(export_name), list)
                    and call.args
                ):
                    value = literal(call.args[0])
                    if call.func.attr == "append" and isinstance(value, str):
                        values[export_name].append(value)
                    elif call.func.attr == "extend" and isinstance(
                        value, (list, tuple)
                    ):
                        values[export_name].extend(
                            x for x in value if isinstance(x, str)
                        )

    hook = definitions.get("__virtual__")
    virtual = values.get("__virtualname__")
    module_names = set()
    if hook:
        for node in ast.walk(hook):
            if isinstance(node, ast.Return):
                value = literal(node.value)
                if isinstance(value, str):
                    module_names.add(value)
        if isinstance(virtual, str):
            module_names.add(virtual)
    if not module_names:
        module_names.add(PurePosixPath(path).stem)
    aliases = values.get("__virtual_aliases__", [])
    if isinstance(aliases, (list, tuple)):
        module_names.update(x for x in aliases if isinstance(x, str))
    func_aliases = values.get("__func_alias__") or {}
    if not isinstance(func_aliases, dict):
        func_aliases = {}
    exports = values.get("__load__", values.get("__all__"))
    restriction = exports if isinstance(exports, (list, tuple)) else None

    resolved = {}

    def resolve(name, seen=()):
        if name in seen or len(seen) > 32:
            return None
        if name in definitions:
            return definitions[name], None, None, path
        node = assignments.get(name)
        if node is None:
            return None
        value = node.value
        target = None
        alias_doc = None
        clone = False
        if isinstance(value, ast.Name):
            target = value.id
        elif isinstance(value, ast.Call):
            called = expression(value.func)
            if (
                isinstance(value.func, ast.Name)
                and value.func.id in references
            ):
                ref_path, ref_name = references[value.func.id]
                called = ref_path[:-3].replace("/", ".") + "." + ref_name
            if called not in (
                "salt.utils.functools.alias_function",
                "salt.utils.functools.namespaced_function",
            ):
                return None
            clone = called.endswith("namespaced_function")
            if value.args and isinstance(value.args[0], ast.Name):
                target = value.args[0].id
                if len(value.args) > 2:
                    alias_doc = literal(value.args[2])
                for kw in value.keywords:
                    if kw.arg == "doc":
                        alias_doc = literal(kw.value)
        if clone and target in references:
            ref_path, ref_name = references[target]
            if ref_path in sources:
                ref_tree = ast.parse(sources[ref_path], filename=ref_path)
                for definition in statements(ref_tree.body):
                    if (
                        isinstance(
                            definition, (ast.FunctionDef, ast.AsyncFunctionDef)
                        )
                        and definition.name == ref_name
                    ):
                        return definition, target, None, ref_path
        if target and (found := resolve(target, (*seen, name))):
            return found[0], target, alias_doc, found[3]
        return None

    excluded = []
    for name in sorted(definitions.keys() | assignments.keys()):
        found = resolve(name)
        if (
            name.startswith("_")
            or restriction is not None
            and name not in restriction
        ):
            if found:
                excluded.append(
                    {
                        "name": name,
                        "reason": (
                            "private"
                            if name.startswith("_")
                            else "literal export restriction"
                        ),
                    }
                )
            continue
        if found:
            resolved[name] = found

    entries = []
    for name, (node, target, alias_doc, defining_path) in resolved.items():
        exposed = func_aliases.get(name, name)
        docs = ast.get_docstring(node) or ""
        if target and defining_path == path:
            docs = (
                alias_doc
                if isinstance(alias_doc, str)
                else f"Alias of {target}.\n\n{docs}"
            )
        for module in sorted(module_names):
            entries.append(
                {
                    "kind": kind,
                    "name": f"{module}.{exposed}",
                    "module": module,
                    "function": exposed,
                    "python_name": name,
                    "provider": path,
                    "line": (
                        assignments[name].lineno if target else node.lineno
                    ),
                    "definition": {
                        "path": defining_path,
                        "line": node.lineno,
                        "name": node.name,
                    },
                    "alias_of": target,
                    "arguments": signature(node),
                    "returns": expression(node.returns),
                    "documentation": docs,
                    "decorators": [expression(d) for d in node.decorator_list],
                    "authoring": kind == "execution"
                    or not exposed.startswith("mod_"),
                    "confidence": "source-declaration; runtime conditions not evaluated",
                }
            )

    dynamic = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = expression(node.func)
            if called in (
                "globals",
                "setattr",
                "exec",
                "eval",
            ) or called.startswith(("__all__.", "__load__.")):
                dynamic.append(
                    {
                        "line": node.lineno,
                        "expression": expression(node),
                        "classification": "dynamic export possibility; not evaluated",
                    }
                )
    unresolved = []
    for name, node in sorted(assignments.items()):
        if name.startswith("_") or name in resolved:
            continue
        if isinstance(node.value, (ast.Call, ast.Name, ast.Attribute)):
            unresolved.append(
                {
                    "name": name,
                    "line": node.lineno,
                    "expression": expression(node.value),
                    "classification": "unresolved value; not assumed callable",
                }
            )
    coverage = {
        "path": path,
        "sha256": digest(source),
        "module_names": sorted(module_names),
        "module_documentation": ast.get_docstring(tree) or "",
        "virtual_hook": expression(hook),
        "literal_exports": restriction,
        "metadata_declarations": [
            {
                "name": name,
                "line": node.lineno,
                "expression": expression(node.value),
                "classification": (
                    "literal" if values.get(name) is not None else "unresolved"
                ),
            }
            for name, node in sorted(assignments.items())
            if name
            in (
                "__virtualname__",
                "__virtual_aliases__",
                "__func_alias__",
                "__all__",
                "__load__",
            )
        ],
        "export_restriction_declared": "__load__" in assignments
        or "__all__" in assignments,
        "entries": len(entries),
        "excluded_functions": excluded,
        "imports": imported,
        "unresolved_assignments": unresolved,
        "dynamic_exports": dynamic,
        "decorated_functions": [
            n for n, (d, _, _, _) in resolved.items() if d.decorator_list
        ],
    }
    return entries, coverage


KINDS = {"state": "salt/states/", "execution": "salt/modules/"}


def generate(pin, sources, kind="state"):
    prefix = KINDS[kind]
    entries = []
    coverage = []
    for path, source in sorted(sources.items()):
        if not path.startswith(prefix):
            continue
        if PurePosixPath(path).name.startswith("_"):
            coverage.append(
                {
                    "path": path,
                    "sha256": digest(source),
                    "classification": "private source, not a loader module",
                }
            )
            continue
        extracted, report = extract_module(path, source, sources, kind)
        entries.extend(extracted)
        coverage.append(report)
    return {
        "schema": SCHEMA,
        "kind": kind,
        "provenance": pin,
        "generator": {
            "path": "tools/generate_catalogs.py",
            "sha256": digest(Path(__file__).read_bytes()),
            "method": "stdlib AST only; no Salt imports or loaders",
        },
        "entries": sorted(
            entries, key=lambda e: (e["name"], e["provider"], e["line"])
        ),
        "coverage": coverage,
        "supporting_sources": [
            {"path": p, "sha256": digest(s)}
            for p, s in sorted(sources.items())
            if not p.startswith(prefix)
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).with_name("catalog-sources.json"),
    )
    parser.add_argument("--archives", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pins = json.loads(args.lock.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    for pin in pins:
        sources = read_archive(args.archives / pin["artifact"], pin["sha256"])
        for kind, name in (("state", "states"), ("execution", "modules")):
            catalog = generate(pin, sources, kind)
            data = (
                json.dumps(
                    catalog,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            buffer = io.BytesIO()
            with gzip.GzipFile(
                filename="", fileobj=buffer, mode="wb", mtime=0
            ) as stream:
                stream.write(data)
            (args.output / f"{name}-{pin['profile']}.json.gz").write_bytes(
                buffer.getvalue()
            )


if __name__ == "__main__":
    main()
