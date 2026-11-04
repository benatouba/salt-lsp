"""Completion and hover for Salt execution calls written inside Jinja.

Template spans are lexed in place, never rendered. Only the literal forms
``salt['mod.func'](...)``, ``__salt__['mod.func'](...)`` and
``salt.mod.func(...)`` are recognized; whatever else a span holds stays unknown.
Every offset here is a codepoint offset into the document source.
"""

from bisect import bisect_right
from dataclasses import dataclass
import re
from typing import Any, Iterator, Optional

from lsprotocol import types
from pygls.workspace import TextDocument

from salt_lsp.catalog import format_signature, load_catalog
from salt_lsp.state_calls import (
    _CLOSERS,
    _NAMED_ARGUMENTS,
    _TEMPLATE_OPEN,
    _argument_item,
    _candidates,
    _describe,
    _end_tag,
    _template_end,
)

_KIND = "execution"
_NAMESPACES = ("salt", "__salt__")
_NAME = re.compile(r"[A-Za-z_]\w*")
_NUMBER = re.compile(r"\d[\w.]*")
_OPERATORS = ("==", "!=", "<=", ">=")
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_OPENING = "([{"
_CLOSING = ")]}"


@dataclass(frozen=True)
class _Token:
    kind: str  # "name", "string", "number" or "op"
    text: str
    start: int
    end: int
    closed: bool = True

    @property
    def content(self) -> tuple[int, int]:
        """Span of a string without its quotes."""
        return self.start + 1, self.end - 1 if self.closed else self.end


def _spans(source: str) -> Iterator[tuple[int, int]]:
    """Yield inner ranges of expressions and tags, skipping raw and comments."""
    position = 0
    while (opened := _TEMPLATE_OPEN.search(source, position)) is not None:
        kind = opened[0][1]
        closer = _CLOSERS[kind]
        end = _template_end(source, opened.end(), closer)
        closed = end - len(closer) >= opened.end() and source.startswith(
            closer, end - len(closer)
        )
        position = end
        if kind == "#":
            continue
        inner_end = end - len(closer) if closed else end
        if kind == "%":
            tag = source[opened.end() : inner_end].strip("-+ \t\r\n").split()
            if tag and tag[0] == "raw":
                raw_end = _end_tag(source, end, "endraw")
                position = raw_end.end() if raw_end else len(source)
                continue
        yield opened.end(), inner_end


def _tokens(source: str, start: int, end: int) -> list[_Token]:
    tokens = []
    index = start
    while index < end:
        character = source[index]
        if character.isspace():
            index += 1
        elif character in "'\"":
            cursor = index + 1
            closed = False
            while cursor < end and source[cursor] not in _LINE_BREAKS:
                if source[cursor] == "\\":
                    cursor += 1
                elif source[cursor] == character:
                    closed = True
                    cursor += 1
                    break
                cursor += 1
            cursor = min(cursor, end)
            tokens.append(
                _Token("string", source[index:cursor], index, cursor, closed)
            )
            index = cursor
        elif match := _NAME.match(source, index):
            end_of_name = min(match.end(), end)
            tokens.append(
                _Token("name", source[index:end_of_name], index, end_of_name)
            )
            index = end_of_name
        elif match := _NUMBER.match(source, index):
            end_of_number = min(match.end(), end)
            tokens.append(
                _Token(
                    "number", source[index:end_of_number], index, end_of_number
                )
            )
            index = end_of_number
        else:
            text = source[index : index + 2]
            text = text if text in _OPERATORS else character
            tokens.append(_Token("op", text, index, index + len(text)))
            index += len(text)
    return tokens


@dataclass(frozen=True)
class _Site:
    """One ``salt`` access, with the tokens that can be edited or documented."""

    string: Optional[_Token] = None
    first_dot: Optional[_Token] = None
    module: Optional[_Token] = None
    second_dot: Optional[_Token] = None
    function: Optional[_Token] = None
    paren: Optional[int] = None  # index of the opening parenthesis


def _is(tokens: list[_Token], index: int, text: str) -> bool:
    return 0 <= index < len(tokens) and tokens[index].text == text


def _name_at(tokens: list[_Token], index: int) -> Optional[_Token]:
    if index < len(tokens) and tokens[index].kind == "name":
        return tokens[index]
    return None


def _sites(tokens: list[_Token]) -> Iterator[_Site]:
    for index, token in enumerate(tokens):
        if (
            token.kind != "name"
            or token.text not in _NAMESPACES
            or _is(tokens, index - 1, ".")
        ):
            continue
        if _is(tokens, index + 1, "["):
            if index + 2 >= len(tokens) or tokens[index + 2].kind != "string":
                continue
            after = index + 3
            if _is(tokens, after, "]") and _is(tokens, after + 1, "("):
                yield _Site(string=tokens[index + 2], paren=after + 1)
            else:
                yield _Site(string=tokens[index + 2])
        elif token.text == "salt" and _is(tokens, index + 1, "."):
            module = _name_at(tokens, index + 2)
            second_dot = None
            function = None
            last = index + 2 if module else index + 1
            if module and _is(tokens, index + 3, "."):
                second_dot = tokens[index + 3]
                function = _name_at(tokens, index + 4)
                last = index + 4 if function else index + 3
            yield _Site(
                first_dot=tokens[index + 1],
                module=module,
                second_dot=second_dot,
                function=function,
                paren=(
                    last + 1
                    if function and _is(tokens, last + 1, "(")
                    else None
                ),
            )


def _name(source: str, site: _Site) -> Optional[str]:
    """The complete function name this site spells, when it is literal."""
    if site.string is not None:
        if not site.string.closed or "\\" in site.string.text:
            return None
        start, end = site.string.content
        return source[start:end]
    if site.module is not None and site.function is not None:
        return f"{site.module.text}.{site.function.text}"
    return None


def _context(
    document: TextDocument, position: types.Position
) -> Optional[tuple[str, int, list[_Token]]]:
    """Tokens of the template span under the cursor, with the cursor offset."""
    lines = document.lines
    if position.line >= len(lines):
        return None
    point = document.position_codec.position_from_client_units(
        lines, types.Position(position.line, position.character)
    )
    offset = sum(len(line) for line in lines[: point.line]) + point.character
    source = document.source
    for start, end in _spans(source):
        if start > offset:
            break
        if offset <= end:
            return source, offset, _tokens(source, start, end)
    return None


def _client_range(document: TextDocument, start: int, end: int) -> types.Range:
    lines = document.lines
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def place(offset: int) -> types.Position:
        line = min(bisect_right(starts, offset) - 1, max(len(lines) - 1, 0))
        return types.Position(line, offset - starts[line])

    return document.position_codec.range_to_client_units(
        lines, types.Range(place(start), place(end))
    )


def _release(version: str) -> str:
    return load_catalog(version, _KIND)["provenance"]["release"]


def hover_execution_call(
    document: TextDocument, position: types.Position, version: str
) -> Optional[types.Hover]:
    """Document the execution function named under the cursor."""
    context = _context(document, position)
    if context is None:
        return None
    source, offset, tokens = context
    for site in _sites(tokens):
        name = _name(source, site)
        if site.string is not None:
            start, end = site.string.content
        elif site.module is not None and site.function is not None:
            start, end = site.module.start, site.function.end
        else:
            continue
        if name is None or not start <= offset <= end:
            continue
        candidates = _candidates(version, name, _KIND)
        if not candidates:
            return None
        release = _release(version)
        sections = [_describe(entry, release) for entry in candidates]
        if len(sections) > 1:
            sections.insert(
                0,
                f"{name}: {len(sections)} conditional provider candidates; "
                "which one a minion loads is not determined here.",
            )
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.PlainText,
                value="\n\n---\n\n".join(sections),
            ),
            range=_client_range(document, start, end),
        )
    return None


def _function_items(
    document: TextDocument,
    version: str,
    prefix: str,
    module: Optional[str],
    span: tuple[int, int],
) -> types.CompletionList:
    release = _release(version)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in load_catalog(version, _KIND)["entries"]:
        if not entry["authoring"]:
            continue
        if module is not None and entry["module"] != module:
            continue
        label = entry["function"] if module is not None else entry["name"]
        if label.startswith(prefix):
            grouped.setdefault(label, []).append(entry)
    edit_range = _client_range(document, *span)
    items = []
    for label, entries in grouped.items():
        first = entries[0]
        providers = ", ".join(f"{e['provider']}:{e['line']}" for e in entries)
        origin = (
            "Project declarations"
            if any(e.get("uri") for e in entries)
            else f"Salt {release}"
        )
        detail = (
            f"{len(entries)} conditional provider candidates — {origin}"
            if len(entries) > 1
            else f"{format_signature(first)} — {origin}; {providers}"
        )
        documentation = (
            f"{origin} source-known execution API ({providers}).\n"
            "Catalog membership is not proof of provider availability. "
            "Runtime gates/dependencies and decorators are not evaluated.\n\n"
            + (
                first["documentation"]
                or "The catalog has no documentation for this."
            )
        )
        items.append(
            types.CompletionItem(
                label=label,
                kind=types.CompletionItemKind.Function,
                detail=detail,
                documentation=documentation,
                text_edit=types.TextEdit(edit_range, label),
                data={
                    "kind": "execution",
                    "version": version,
                    "name": first["name"],
                    "providers": [e["provider"] for e in entries],
                },
            )
        )
    return types.CompletionList(is_incomplete=False, items=items)


def _module_items(
    document: TextDocument,
    version: str,
    prefix: str,
    span: tuple[int, int],
    suffix: str,
) -> types.CompletionList:
    release = _release(version)
    modules = sorted(
        {
            e["module"]
            for e in load_catalog(version, _KIND)["entries"]
            if e["authoring"] and e["module"].startswith(prefix)
        }
    )
    edit_range = _client_range(document, *span)
    return types.CompletionList(
        is_incomplete=False,
        items=[
            types.CompletionItem(
                label=name,
                kind=types.CompletionItemKind.Module,
                detail=f"Salt {release} source-known execution module",
                documentation=(
                    "Catalog membership is not proof of provider availability."
                ),
                text_edit=types.TextEdit(edit_range, name + suffix),
            )
            for name in modules
        ],
    )


def _argument_names(
    document: TextDocument,
    version: str,
    source: str,
    tokens: list[_Token],
    site: _Site,
    offset: int,
) -> Optional[types.CompletionList]:
    """Offer keyword names where an argument of the call is being started."""
    name = _name(source, site)
    candidates = _candidates(version, name, _KIND) if name else []
    if site.paren is None or not candidates:
        return None
    depth = 1
    previous = tokens[site.paren]
    current: Optional[int] = None
    for index in range(site.paren + 1, len(tokens)):
        token = tokens[index]
        if token.kind == "name" and token.start <= offset <= token.end:
            current = index
            break
        if token.start >= offset:
            break
        if token.kind == "string" and offset <= token.end:
            return None
        if token.text in _OPENING:
            depth += 1
        elif token.text in _CLOSING:
            depth -= 1
            if depth == 0:
                return None
        previous = token
    if depth != 1 or previous.text not in ("(", ","):
        return None

    specified = set()
    depth = 1
    for index in range(site.paren + 1, len(tokens)):
        token = tokens[index]
        if token.text in _OPENING:
            depth += 1
        elif token.text in _CLOSING:
            depth -= 1
            if depth == 0:
                break
        elif (
            depth == 1
            and token.kind == "name"
            and index != current
            and _is(tokens, index + 1, "=")
        ):
            specified.add(token.text)

    start = end = offset
    prefix = ""
    equals = "="
    if current is not None:
        token = tokens[current]
        start, end = token.start, token.end
        prefix = token.text[: offset - token.start]
        if _is(tokens, current + 1, ("(")) or _is(tokens, current + 1, "."):
            return None
        if _is(tokens, current + 1, "="):
            equals = ""
    arguments: dict[str, list[tuple[dict[str, Any], dict[str, Any], int]]] = {}
    for candidate in candidates:
        for index, argument in enumerate(candidate["arguments"]):
            argument_name = argument["name"]
            if (
                argument["kind"] in _NAMED_ARGUMENTS
                and not argument_name.startswith("_")
                and argument_name not in specified
                and argument_name.startswith(prefix)
            ):
                arguments.setdefault(argument_name, []).append(
                    (candidate, argument, index)
                )
    edit_range = _client_range(document, start, end)
    release = _release(version)
    return types.CompletionList(
        is_incomplete=False,
        items=[
            _argument_item(
                argument_name,
                declared_by,
                (name or "", version, release),
                len(candidates) > 1,
                types.TextEdit(edit_range, argument_name + equals),
            )
            for argument_name, declared_by in arguments.items()
        ],
    )


def _site_completion(
    document: TextDocument,
    version: str,
    source: str,
    offset: int,
    site: _Site,
) -> Optional[types.CompletionList]:
    string = site.string
    if string is not None:
        start, end = string.content
        if start <= offset <= end and "\\" not in string.text:
            return _function_items(
                document, version, source[start:offset], None, (start, end)
            )
        return None
    first, module = site.first_dot, site.module
    second, function = site.second_dot, site.function
    assert first is not None
    if module is None:
        if offset == first.end:
            return _module_items(document, version, "", (offset, offset), ".")
        return None
    if first.end <= offset <= module.end:
        following = source[module.end : module.end + 1]
        return _module_items(
            document,
            version,
            module.text[: offset - module.start],
            (module.start, module.end),
            "" if following == "." else ".",
        )
    if second is None or offset < second.end:
        return None
    if function is None:
        if offset == second.end:
            return _function_items(
                document, version, "", module.text, (offset, offset)
            )
        return None
    if offset <= function.end:
        return _function_items(
            document,
            version,
            function.text[: offset - function.start],
            module.text,
            (function.start, function.end),
        )
    return None


def complete_execution_calls(
    document: TextDocument, position: types.Position, version: str
) -> Optional[types.CompletionList]:
    """Complete execution functions and their arguments inside templates."""
    context = _context(document, position)
    if context is None:
        return None
    source, offset, tokens = context
    for site in _sites(tokens):
        result = _site_completion(document, version, source, offset, site)
        if result is None and site.paren is not None:
            result = _argument_names(
                document, version, source, tokens, site, offset
            )
        if result is not None:
            return result
    return None
