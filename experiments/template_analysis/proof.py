"""Non-rendering experiment, deliberately independent of the language server."""

import json
import re
import sys

from jinja2 import Environment, TemplateSyntaxError
import yaml


def analyze(source):
    result = {
        "declarations": [],
        "references": [],
        "unknowns": [],
        "comparisons": {},
    }
    if source.startswith("#!") and source.splitlines()[0] not in {
        "#!jinja|yaml"
    }:
        result["unknowns"].append(
            location(
                source, "unsupported_renderer", 0, len(source.splitlines()[0])
            )
        )
        return result
    try:
        yaml.compose(source, Loader=yaml.SafeLoader)
        result["comparisons"]["raw_yaml"] = {"accepted": True}
    except yaml.YAMLError as error:
        result["comparisons"]["raw_yaml"] = {
            "accepted": False,
            "error": type(error).__name__,
        }
    try:
        # Syntax only: never compile, render, load a template, or add globals.
        Environment().parse(source)
        result["comparisons"]["jinja_syntax"] = {"accepted": True}
    except TemplateSyntaxError as error:
        result["comparisons"]["jinja_syntax"] = {
            "accepted": False,
            "error": error.message,
            "line": error.lineno,
        }

    projected, spans, scopes = project(source, result)

    def emit(kind, node, base, certainty="literal"):
        start = base + node.start_mark.index
        end = base + node.end_mark.index
        if any(a < end and b > start for a, b in spans):
            result["unknowns"].append(
                location(
                    source,
                    (
                        "generated_key"
                        if kind in {"state", "function"}
                        else "generated_reference"
                    ),
                    start,
                    end,
                )
            )
            return False
        if not isinstance(node, yaml.ScalarNode):
            result["unknowns"].append(
                location(source, "complex_key", start, end)
            )
            return False
        if any(a <= start < b for a, b in scopes):
            certainty = "conditional"
        item = location(source, kind, start, end)
        item.update(value=node.value, certainty=certainty)
        target = "references" if kind == "sls_include" else "declarations"
        result[target].append(item)
        return True

    # Independent top-level islands bound scanner damage to one declaration.
    starts = [m.start() for m in re.finditer(r"(?m)^[^\s#].*", projected)]
    starts.append(len(projected))
    for base, stop in zip(starts, starts[1:]):
        chunk = projected[base:stop]
        try:
            root = yaml.compose(chunk, Loader=yaml.SafeLoader)
        except yaml.YAMLError:
            result["unknowns"].append(
                location(source, "malformed_yaml", base, stop)
            )
            # Only syntactically complete mapping headers survive a broken body.
            headers = []
            for match in re.finditer(r"(?m)^(?:  )?[^\s#].*:\s*$", chunk):
                try:
                    node = yaml.compose(match.group(), Loader=yaml.SafeLoader)
                    if isinstance(node, yaml.MappingNode):
                        key, _ = node.value[0]
                        # compose() marks include indentation; add line offset.
                        headers.append(
                            (
                                key,
                                match.start(),
                                (
                                    "function"
                                    if match.group().startswith("  ")
                                    else "state"
                                ),
                            )
                        )
                except yaml.YAMLError:
                    pass
            for key, offset, kind in headers:
                if kind == "state" and key.value in {
                    "include",
                    "extend",
                    "exclude",
                }:
                    break
                emit(kind, key, base + offset, "partial")
            continue
        if not isinstance(root, yaml.MappingNode):
            result["unknowns"].append(
                location(source, "unsupported_yaml", base, stop)
            )
            continue
        for key, value in root.value:
            if isinstance(key, yaml.ScalarNode) and key.value in {
                "extend",
                "exclude",
            }:
                result["unknowns"].append(
                    location(source, "reserved_key", base, stop)
                )
                continue
            if isinstance(key, yaml.ScalarNode) and key.value == "include":
                if isinstance(value, yaml.SequenceNode):
                    for ref in value.value:
                        emit("sls_include", ref, base)
                continue
            literal = emit("state", key, base)
            if isinstance(value, yaml.MappingNode):
                for function, _ in value.value:
                    emit(
                        "function",
                        function,
                        base,
                        "literal" if literal else "conditional",
                    )
    return result


def location(source, kind, start, end):
    return {
        "kind": kind,
        "start": start,
        "end": end,
        "line": source.count("\n", 0, start),
        "column": start - source.rfind("\n", 0, start) - 1,
    }


def project(source, result):
    """Position-preserving analysis projection, NOT a formatting transform."""
    chars = list(source)
    spans, scopes, stack = [], [], []
    cursor = 0
    while match := re.search(r"{{|{%|{#", source[cursor:]):
        start = cursor + match.start()
        opener = match.group()
        closer = {"{{": "}}", "{%": "%}", "{#": "#}"}[opener]
        end = delimiter_end(source, start + 2, closer, opener == "{#")
        complete = end >= 0
        end = end + 2 if complete else source.find("\n", start)
        if end < 0:
            end = len(source)
        if not complete:
            result["unknowns"].append(
                location(source, "incomplete_template", start, end)
            )
        spans.append((start, end))
        body = source[start + 2 : end - 2].strip("-+ \r\n")
        if opener == "{{":
            result["unknowns"].append(
                location(source, "expression", start, end)
            )
        if opener == "{%":
            command = body.split(maxsplit=1)[0] if body else ""
            if command in {"if", "for", "macro", "call", "filter", "block"}:
                stack.append((command, start))
            elif command.startswith("end") and stack:
                _, scope_start = stack.pop()
                scopes.append((scope_start, end))
            reference = re.fullmatch(
                r"(import|from|include)\s+(['\"])([^'\"\\\n]+)\2"
                r"(?:\s+(?:as\s+\w+|import\s+[\w, ]+|with context|without context))?",
                body,
            )
            if reference:
                value = reference.group(3)
                a = source.find(reference.group(2) + value, start) + 1
                item = location(source, reference.group(1), a, a + len(value))
                item.update(value=value, certainty="literal")
                result["references"].append(item)
            elif command in {"import", "from", "include"}:
                result["unknowns"].append(
                    location(source, "dynamic_reference", start, end)
                )
            result["unknowns"].append(location(source, "control", start, end))
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = "x" if opener == "{{" else " "
        cursor = end
        if opener == "{%" and body == "raw":
            raw_end = re.search(r"{%[-+]?\s*endraw\s*[-+]?%}", source[cursor:])
            if raw_end:
                # Raw contents are literal YAML, not Jinja tokens.
                cursor += raw_end.start()
            else:
                result["unknowns"].append(
                    location(source, "incomplete_template", start, len(source))
                )
                break
    scopes.extend((start, len(source)) for _, start in stack)
    return "".join(chars), spans, scopes


def delimiter_end(source, start, closer, comment=False):
    """Recognize quoted closers and balanced expression containers, without eval."""
    quote, escaped, brackets = None, False, []
    index = start
    while index < len(source):
        char = source[index]
        if comment:
            return source.find(closer, index)
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif not brackets and source.startswith(closer, index):
            return index
        elif char in "'\"":
            quote = char
        elif char in "([{":
            brackets.append(char)
        elif char in ")]}":
            if (
                brackets
                and brackets[-1] == {")": "(", "]": "[", "}": "{"}[char]
            ):
                brackets.pop()
        index += 1
    return -1


if __name__ == "__main__":
    print(
        json.dumps(
            analyze(sys.stdin.buffer.read().decode("utf-8")),
            ensure_ascii=False,
            indent=2,
        )
    )
