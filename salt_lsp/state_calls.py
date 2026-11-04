"""Hover and argument assistance for state calls recognized in source text.

Documents are usually incomplete and mix YAML with Jinja while being edited, so
this reads a position-preserving projection of the source line by line rather
than parsed YAML. Templates are lexed, never rendered. Whatever cannot be placed
in a literal state call stays unknown and yields no assistance.
"""

from dataclasses import dataclass
import re
import textwrap
from typing import Any, Optional

from lsprotocol import types
from pygls.workspace import TextDocument

from salt_lsp.catalog import format_signature, load_catalog

#: Stands in for expression text: it passes as part of a generated state ID but
#: can never be part of a function or argument name.
_UNKNOWN = "�"
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85  "
_TEMPLATE_OPEN = re.compile(r"\{[%{#]")
_CLOSERS = {"%": "%}", "{": "}}", "#": "#}"}
#: Tags whose body is captured into a variable instead of being emitted.
_CAPTURE_END_TAGS = {
    "load_yaml": "endload",
    "load_json": "endload",
    "load_text": "endload",
    "set": "endset",
}

_CALL_KEY = re.compile(r"( +)([A-Za-z_]\w*(?:\.\w+)?)(\s*:(?:\s.*)?)?\s*")
_STATE_ID = re.compile(r"[^#{}\[\]\s-].*:\s*(?:#.*)?")
_EMPTY_VALUE = re.compile(r"\s*:\s*(?:#.*)?")
_ARGUMENT_ENTRY = re.compile(r"( *)-( *)([A-Za-z_]\w*)?(\s*:(?:\s.*)?)?\s*")
_STRING_BODY = re.compile(r"([^\s#-].*?):\s+([A-Za-z_]\w*\.\w+)\s*(?:#.*)?")
_RESERVED_KEYS = ("include", "exclude", "extend")
_NAMED_ARGUMENTS = ("positional_or_keyword", "keyword_only")


def _template_end(source: str, start: int, closer: str) -> int:
    """Find where a template span ends, tolerating spans still being typed.

    An unclosed span ends with its line, so that it hides nothing further down.
    """
    line_end = start
    while line_end < len(source) and source[line_end] not in _LINE_BREAKS:
        line_end += 1
    quote = None
    index = start
    while index < len(source):
        character = source[index]
        if character in _LINE_BREAKS:
            quote = None
        elif quote is not None:
            if character == "\\":
                index += 1
            elif character == quote:
                quote = None
        elif closer != "#}" and character in "'\"":
            quote = character
        elif source.startswith(closer, index):
            return index + len(closer)
        elif closer != "#}" and _TEMPLATE_OPEN.match(source, index):
            return line_end
        index += 1
    return line_end


def _end_tag(source: str, start: int, name: str) -> Optional[re.Match[str]]:
    return re.compile(rf"\{{%[-+]?\s*{name}\s*[-+]?%\}}").search(source, start)


def _project_template(source: str) -> str:
    """Blank template syntax while retaining every line and column.

    Control tags and comments become whitespace, expressions become unknown
    text, captured bodies disappear and raw bodies stay literal.
    """
    output = list(source)

    def fill(start: int, end: int, character: str) -> None:
        for index in range(start, end):
            if output[index] not in _LINE_BREAKS:
                output[index] = character

    position = 0
    while (opened := _TEMPLATE_OPEN.search(source, position)) is not None:
        kind = opened[0][1]
        end = _template_end(source, opened.end(), _CLOSERS[kind])
        fill(opened.start(), end, _UNKNOWN if kind == "{" else " ")
        position = end
        if kind != "%":
            continue
        tag = source[opened.end() : end].strip("-+%} \t\r\n").split()
        name = tag[0] if tag else ""
        if name == "raw":
            if (raw_end := _end_tag(source, end, "endraw")) is not None:
                fill(raw_end.start(), raw_end.end(), " ")
                position = raw_end.end()
        elif name in _CAPTURE_END_TAGS and "=" not in "".join(tag[1:]):
            captured = _end_tag(source, end, _CAPTURE_END_TAGS[name])
            if captured is not None:
                fill(end, captured.end(), " ")
                position = captured.end()
    return "".join(output)


def _is_blank(line: str) -> bool:
    stripped = line.replace(_UNKNOWN, "").strip()
    return not stripped or stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_sequence_entry(line: str) -> bool:
    stripped = line.lstrip(" ")
    return stripped == "-" or stripped.startswith("- ")


def _parent(lines: list[str], index: int) -> Optional[int]:
    width = _indent(lines[index])
    for parent in range(index - 1, -1, -1):
        if not _is_blank(lines[parent]) and _indent(lines[parent]) < width:
            return parent
    return None


def _is_state_id_key(lines: list[str], index: Optional[int]) -> bool:
    """Whether a line declares a state ID, directly or inside ``extend``."""
    if index is None or not _STATE_ID.fullmatch(lines[index].lstrip(" ")):
        return False
    if _indent(lines[index]) == 0:
        return lines[index].split(":", 1)[0] not in _RESERVED_KEYS
    extend = _parent(lines, index)
    return (
        extend is not None
        and _indent(lines[extend]) == 0
        and _STATE_ID.fullmatch(lines[extend]) is not None
        and lines[extend].split(":", 1)[0] == "extend"
    )


def _sequence_owner(lines: list[str], index: int) -> Optional[int]:
    """Find the mapping key that owns the sequence entry at ``index``."""
    column = _indent(lines[index])
    for owner in range(index - 1, -1, -1):
        line = lines[owner]
        if _is_blank(line) or _indent(line) > column:
            continue
        if _indent(line) == column and _is_sequence_entry(line):
            continue
        return None if _is_sequence_entry(line) else owner
    return None


def _sequence_entries(
    lines: list[str], owner: int
) -> dict[int, Optional[re.Match[str]]]:
    """Map line numbers of the sequence under a key to their parsed entries."""
    column = None
    entries: dict[int, Optional[re.Match[str]]] = {}
    for index in range(owner + 1, len(lines)):
        line = lines[index]
        if _is_blank(line):
            continue
        if column is None:
            if not _is_sequence_entry(line) or _indent(line) < _indent(
                lines[owner]
            ):
                break
            column = _indent(line)
        if _indent(line) > column:
            continue
        if _indent(line) < column or not _is_sequence_entry(line):
            break
        entries[index] = _ARGUMENT_ENTRY.fullmatch(line)
    return entries


def _function_name(
    lines: list[str], key: int, version: str, typing_at: Optional[int] = None
) -> Optional[str]:
    """Name the function of a state call key, in either source form.

    ``file.managed:`` names it directly. ``file:`` names it through the single
    plain scalar in its sequence that the catalog knows, because an unfinished
    entry is a plain scalar too. Zero or several leave the function unknown.
    """
    match = _CALL_KEY.fullmatch(lines[key])
    if match is None or not _is_state_id_key(lines, _parent(lines, key)):
        return None
    if "." in match[2]:
        return match[2]
    if match[3] is not None and not _EMPTY_VALUE.fullmatch(match[3]):
        return None
    functions = [
        f"{match[2]}.{entry[3]}"
        for index, entry in _sequence_entries(lines, key).items()
        if index != typing_at and entry and entry[3] and entry[4] is None
    ]
    known = [name for name in functions if _candidates(version, name)]
    return known[0] if len(known) == 1 else None


@dataclass(frozen=True)
class _Call:
    """A state call token in the source, in codepoint columns."""

    function: str
    start: int
    end: int


def _call_under_cursor(
    lines: list[str], line: int, character: int, version: str
) -> Optional[_Call]:
    text = lines[line]
    if (body := _STRING_BODY.fullmatch(text)) is not None:
        if (
            body.start(2) <= character < body.end(2)
            and body[1] not in _RESERVED_KEYS
        ):
            return _Call(body[2], body.start(2), body.end(2))
        return None
    if (key := _CALL_KEY.fullmatch(text)) is not None:
        if key.start(2) <= character < key.end(2):
            function = _function_name(lines, line, version)
            if function is not None:
                return _Call(function, key.start(2), key.end(2))
        return None
    entry = _ARGUMENT_ENTRY.fullmatch(text)
    if (
        entry is None
        or not entry[2]
        or not entry[3]
        or entry[4] is not None
        or not entry.start(3) <= character < entry.end(3)
    ):
        return None
    owner = _sequence_owner(lines, line)
    if owner is None:
        return None
    module = _CALL_KEY.fullmatch(lines[owner])
    function = _function_name(lines, owner, version)
    if (
        module is None
        or "." in module[2]
        or function != f"{module[2]}.{entry[3]}"
    ):
        return None
    return _Call(function, entry.start(3), entry.end(3))


def _candidates(
    version: str, name: str, kind: str = "state"
) -> list[dict[str, Any]]:
    return [
        e for e in load_catalog(version, kind)["entries"] if e["name"] == name
    ]


def _describe(entry: dict[str, Any], release: str) -> str:
    provider = f"{entry['provider']}:{entry['line']}"
    alias = f"Alias of {entry['alias_of']}.\n" if entry["alias_of"] else ""
    documentation = (
        entry["documentation"] or "The catalog has no documentation for this."
    )
    return (
        f"{format_signature(entry)}\n\n"
        f"Salt {release} source-known {entry['kind']} API ({provider}).\n{alias}"
        "Catalog membership is not proof of provider availability. "
        "Runtime gates/dependencies and decorators are not evaluated.\n\n"
        f"{documentation}"
    )


def _cursor(
    document: TextDocument, position: types.Position
) -> Optional[tuple[list[str], types.Position]]:
    """Return projected lines without terminators and the codepoint cursor."""
    if position.line >= len(document.lines):
        return None
    point = document.position_codec.position_from_client_units(
        document.lines, types.Position(position.line, position.character)
    )
    lines = _project_template(document.source).splitlines()
    return lines, point


def hover_state_call(
    document: TextDocument, position: types.Position, version: str
) -> Optional[types.Hover]:
    """Document the recognized state call under the cursor."""
    cursor = _cursor(document, position)
    if cursor is None:
        return None
    lines, point = cursor
    call = _call_under_cursor(lines, point.line, point.character, version)
    if call is None:
        return None
    candidates = _candidates(version, call.function)
    if not candidates:
        return None
    release = load_catalog(version)["provenance"]["release"]
    sections = [_describe(entry, release) for entry in candidates]
    if len(sections) > 1:
        sections.insert(
            0,
            f"{call.function}: {len(sections)} conditional provider "
            "candidates; which one a minion loads is not determined here.",
        )
    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.PlainText,
            value="\n\n---\n\n".join(sections),
        ),
        range=document.position_codec.range_to_client_units(
            document.lines,
            types.Range(
                types.Position(point.line, call.start),
                types.Position(point.line, call.end),
            ),
        ),
    )


def _argument_documentation(documentation: str, name: str) -> Optional[str]:
    """Extract one argument's prose from a docstring when it is recognizable.

    Salt docstrings mostly use definition lists, ``name : default``, Google
    style ``name (type):`` or ``:param type name:``. Prose that merely starts
    with the name, or an indented YAML example, must not match.
    """
    escaped = re.escape(name)
    header = re.compile(
        rf"(?:{escaped}(?:\s*\([^)]*\))?(?:\s*(?::|[-–]\s)\s*(?P<a>.*))?"
        rf"|\s{{4}}{escaped}\s*\([^)]*\):\s*(?P<b>.*)"
        rf"|\s*:param(?:\s+[^:]+?)?\s+{escaped}:\s*(?P<c>.*))"
    )
    lines = documentation.splitlines()
    for index, line in enumerate(lines):
        match = header.fullmatch(line.rstrip())
        if match is None:
            continue
        inline = match["a"] or match["b"] or match["c"] or ""
        width = _indent(line)
        body = []
        for following in lines[index + 1 :]:
            if following.strip() and _indent(following) <= width:
                break
            body.append(following)
        text = f"{inline}\n{textwrap.dedent(chr(10).join(body))}".strip()
        if text:
            return text
    return None


@dataclass(frozen=True)
class _ArgumentSlot:
    """Where an argument name is being typed, in codepoint columns."""

    prefix: str
    start: int
    end: int
    lead: str
    suffix: str


def _argument_slot(
    source_line: str, entry: re.Match[str], character: int
) -> Optional[_ArgumentSlot]:
    suffix = "" if entry[4] else ": "
    if entry[3] is None:
        dash_end = entry.end(1) + 1
        if (
            entry[4]
            or source_line.strip() != "-"
            or not dash_end <= character <= entry.end(2)
        ):
            return None
        lead = " " if character == dash_end else ""
        return _ArgumentSlot("", character, character, lead, suffix)
    # ``-name`` is a plain scalar rather than a sequence entry.
    if not entry[2] or not entry.start(3) <= character <= entry.end(3):
        return None
    prefix = entry[3][: character - entry.start(3)]
    return _ArgumentSlot(prefix, entry.start(3), entry.end(3), "", suffix)


def _argument_label(argument: dict[str, Any]) -> str:
    label = argument["name"]
    if argument["annotation"]:
        label += f": {argument['annotation']}"
    if argument["has_default"]:
        return f"{label}={argument['default']}"
    return f"{label} (required)"


def _argument_item(
    name: str,
    declared_by: list[tuple[dict[str, Any], dict[str, Any], int]],
    context: tuple[str, str, str],
    several: bool,
    edit: types.TextEdit,
) -> types.CompletionItem:
    """Describe one name, keeping the provider of each conflicting candidate."""
    function, version, release = context
    labels = [
        _argument_label(argument)
        + (f" [{entry['provider']}]" if several else "")
        for entry, argument, _ in declared_by
    ]
    documents: dict[str, str] = {}
    for entry, _, _ in declared_by:
        text = _argument_documentation(entry["documentation"], name)
        if text:
            documents.setdefault(text, entry["provider"])
    documentation = "\n\n".join(
        f"{provider}:\n{text}" if several else text
        for text, provider in documents.items()
    )
    required = any(
        not argument["has_default"] for _, argument, _ in declared_by
    )
    position = min(index for _, _, index in declared_by)
    return types.CompletionItem(
        label=name,
        kind=types.CompletionItemKind.Property,
        detail=f"{'; '.join(labels)} — {function}, Salt {release}",
        documentation=documentation or None,
        sort_text=f"{0 if required else 1}{position:04}",
        text_edit=edit,
        data={
            "kind": f"{declared_by[0][0]['kind']}-argument",
            "version": version,
            "function": function,
            "providers": [entry["provider"] for entry, _, _ in declared_by],
        },
    )


def complete_arguments(
    document: TextDocument, position: types.Position, version: str
) -> Optional[types.CompletionList]:
    """Complete argument names of the literal state call owning a list entry."""
    cursor = _cursor(document, position)
    if cursor is None:
        return None
    lines, point = cursor
    entry = _ARGUMENT_ENTRY.fullmatch(lines[point.line])
    if entry is None:
        return None
    slot = _argument_slot(document.lines[point.line], entry, point.character)
    owner = _sequence_owner(lines, point.line)
    if slot is None or owner is None:
        return None
    key = _CALL_KEY.fullmatch(lines[owner])
    if key is None or (
        key[3] is not None and not _EMPTY_VALUE.fullmatch(key[3])
    ):
        return None
    siblings = _sequence_entries(lines, owner)
    if point.line not in siblings:
        return None
    function = _function_name(lines, owner, version, typing_at=point.line)
    candidates = _candidates(version, function) if function else []
    if not candidates or function is None:
        return None
    specified = {
        sibling[3]
        for index, sibling in siblings.items()
        if index != point.line and sibling and sibling[3]
    }
    arguments: dict[str, list[tuple[dict[str, Any], dict[str, Any], int]]] = {}
    for candidate in candidates:
        for index, argument in enumerate(candidate["arguments"]):
            name = argument["name"]
            if (
                argument["kind"] in _NAMED_ARGUMENTS
                and not name.startswith("_")
                and name not in specified
                and name.startswith(slot.prefix)
            ):
                arguments.setdefault(name, []).append(
                    (candidate, argument, index)
                )
    edit_range = document.position_codec.range_to_client_units(
        document.lines,
        types.Range(
            types.Position(point.line, slot.start),
            types.Position(point.line, slot.end),
        ),
    )
    release = load_catalog(version)["provenance"]["release"]
    return types.CompletionList(
        is_incomplete=False,
        items=[
            _argument_item(
                name,
                declared_by,
                (function, version, release),
                len(candidates) > 1,
                types.TextEdit(edit_range, f"{slot.lead}{name}{slot.suffix}"),
            )
            for name, declared_by in arguments.items()
        ],
    )
