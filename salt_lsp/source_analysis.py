"""Recover independent top-level source stanzas in a non-rendered projection."""

import re

from salt_lsp.parser import Position, Tree, parse
from salt_lsp.state_calls import _project_template


def source_tree(source: str) -> Tree:
    """Keep damage in one stanza from hiding subsequent source declarations."""
    lines = _project_template(source).splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"[^\s#%{}\[\]-].*:", line)
    ]
    tree = Tree()
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        stanza = parse("".join(lines[start:end]))

        def shift(node):
            if node.start is not None:
                node.start = Position(node.start.line + start, node.start.col)
            if node.end is not None:
                node.end = Position(node.end.line + start, node.end.col)
            return True

        stanza.visit(shift)
        tree.states.extend(stanza.states)
        if stanza.includes:
            if tree.includes:
                tree.includes.includes.extend(stanza.includes.includes)
            else:
                tree.includes = stanza.includes
        if stanza.extend:
            if tree.extend:
                tree.extend.states.extend(stanza.extend.states)
            else:
                tree.extend = stanza.extend
    return tree
