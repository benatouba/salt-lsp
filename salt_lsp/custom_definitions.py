"""Standard cross-language locations, computed from the current source snapshot."""

from lsprotocol import types

from salt_lsp.catalog import load_catalog
from salt_lsp.jinja_calls import _context, _sites, _name
from salt_lsp.state_calls import _cursor, _call_under_cursor


def definitions(workspace, document, position, version):
    """Resolve literal state/Jinja calls without claiming generic Python features."""
    name = None
    kind = "state"
    context = _context(document, position)
    if context:
        source, offset, tokens = context
        for site in _sites(tokens):
            if site.string:
                start, end = site.string.content
            elif site.module and site.function:
                start, end = site.module.start, site.function.end
            else:
                continue
            if start <= offset <= end:
                name, kind = _name(source, site), "execution"
                break
    if name is None:
        cursor = _cursor(document, position)
        if cursor:
            lines, point = cursor
            if point.line < len(lines):
                call = _call_under_cursor(
                    lines, point.line, point.character, version
                )
                name = call.function if call else None
    results = []
    for entry in load_catalog(version, kind)["entries"]:
        if entry["name"] != name or not entry.get("uri"):
            continue
        line, start, end = entry["source_range"]
        target = workspace.get_text_document(entry["uri"])
        location = types.Location(
            entry["uri"],
            target.position_codec.range_to_client_units(
                target.lines,
                types.Range(
                    types.Position(line, start), types.Position(line, end)
                ),
            ),
        )
        if location not in results:
            results.append(location)
    return results or None
