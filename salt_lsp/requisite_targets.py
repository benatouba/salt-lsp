"""Literal requisite candidates in the known include closure, not a compiler."""

from fnmatch import fnmatchcase

import yaml
from lsprotocol import types

from salt_lsp.parser import RequisiteNode, parse
from salt_lsp.project import file_path
from salt_lsp.source_references import SourceView, Reference
from salt_lsp.state_calls import _project_template, _UNKNOWN
from salt_lsp.utils import ast_node_to_range, construct_path_to_position


def _literal(value):
    return (
        isinstance(value, str)
        and bool(value)
        and _UNKNOWN not in value
        and not any(c in value for c in "\x00\n\r")
    )


def _names(call):
    for parameter in call.parameters:
        if parameter.name == "name" and _literal(parameter.value):
            yield parameter.value
        elif parameter.name == "names" and isinstance(parameter.value, list):
            for node in parameter.value:
                if isinstance(node.token, yaml.ScalarToken) and _literal(
                    node.token.value
                ):
                    yield node.token.value


def candidates(workspace, document, position, context):
    """Return all justified source declarations, preserving branch ambiguity."""
    tree = parse(_project_template(document.source))
    path = construct_path_to_position(tree, position, document)
    if not path or not isinstance(path[-1], RequisiteNode):
        return None
    requisite = path[-1]
    target = requisite.reference
    if not _literal(target):
        return None
    view = SourceView(workspace, context)
    origin = file_path(document.uri)
    closure = list(view.closure(origin))
    sls_paths = set()
    if requisite.module == "sls":
        if any(c in target for c in "*?["):
            for source, _ in closure:
                for root in view.roots:
                    if source.is_relative_to(root):
                        parts = list(
                            source.relative_to(root).with_suffix("").parts
                        )
                        if parts[-1] == "init":
                            parts.pop()
                        if fnmatchcase(".".join(parts), target):
                            sls_paths.add(source)
        else:
            sls_paths.update(view.resolve(origin, Reference(target, 0, 0, 0)))
    locations = []
    for source, source_document in closure:
        for state in parse(_project_template(source_document.source)).states:
            if not _literal(state.identifier):
                continue
            matches = requisite.module == "sls" and source in sls_paths
            for call in state.states:
                if not call.name or (
                    requisite.module
                    and requisite.module
                    not in ("sls", call.name.split(".", 1)[0])
                ):
                    continue
                if requisite.module != "sls" and any(
                    fnmatchcase(name, target)
                    for name in (state.identifier, *_names(call))
                ):
                    matches = True
            if (
                matches
                and (span := ast_node_to_range(state, source_document))
                is not None
            ):
                location = types.Location(source.as_uri(), span)
                if location not in locations:
                    locations.append(location)
    if len(locations) == 1:
        return locations[0]
    return locations or None
