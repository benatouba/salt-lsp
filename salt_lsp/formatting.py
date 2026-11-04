"""Conservative stdin/stdout formatting; never hand a tool a writable source."""

import yaml
from lsprotocol import types


def plain_yaml(source):
    """Only the verified standard YAML subset, not a renderer capability claim."""
    if any(marker in source for marker in ("{{", "{%", "{#")):
        return False
    first = source.splitlines()[0] if source else ""
    if first.startswith("#!") and first not in ("#!yaml", "#!jinja|yaml"):
        return False
    try:
        # Even a syntactically valid YAML tag may require an arbitrary renderer.
        return not any(
            isinstance(token, (yaml.TagToken, yaml.DirectiveToken))
            for token in yaml.scan(source)
        )
    except (yaml.YAMLError, RecursionError):
        return False


async def format_document(server, params):
    workspace = server.workspace
    uri = params.text_document.uri
    document = workspace.text_documents.get(uri)
    context = workspace.project_context(uri)

    def refuse(message):
        server.window_show_message(
            types.ShowMessageParams(
                type=types.MessageType.Warning,
                message="Salt formatting: " + message,
            )
        )
        return []

    if not document or not context or context.file_role != "state":
        return refuse(
            "only open state documents with a selected profile are supported."
        )
    source = document.source
    if len(source.encode("utf-8")) > 1024 * 1024 or not plain_yaml(source):
        return refuse(
            "only bounded, standard plain YAML is verified; Jinja, custom renderers, tags and directives are not formatted."
        )
    stamp = workspace.document_stamp(uri)

    def current():
        return workspace.document_stamp(uri), workspace.project_context(uri)

    snapshot = stamp, context
    probe = await server.tools.probe(uri, "yamlfmt")
    if current() != snapshot:
        return []
    if probe["status"] != "clean":
        return refuse(probe["message"])
    result = await server.tools.run(
        uri,
        "yamlfmt",
        lambda selection, config: [
            "-conf",
            str(config),
            "-no_global_conf",
            "-in",
        ],
        version=snapshot,
        current_version=current,
        data=source.encode("utf-8"),
        output_limit=2 * 1024 * 1024,
    )
    if current() != snapshot or result.status in ("stale", "cancelled"):
        return []
    if result.status != "clean":
        return refuse(result.message)
    try:
        formatted = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return refuse("formatter returned invalid UTF-8; no edits were made.")
    if formatted == source:
        return []
    # PositionCodec performs negotiated UTF-8/16/32 conversion, including emoji.
    lines = source.split("\n")
    end = document.position_codec.position_to_client_units(
        document.lines, types.Position(len(lines) - 1, len(lines[-1]))
    )
    return [types.TextEdit(types.Range(types.Position(0, 0), end), formatted)]
