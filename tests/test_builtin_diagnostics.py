"""Conservative findings and clearing at the real protocol boundary."""

from test_projects import clients  # pylint: disable=unused-import
from test_source_references import settings
from test_jinja_call_assistance import change


def diagnostics(client, path):
    client.symbols(path)
    messages = [
        item["params"]
        for item in client.notifications
        if item.get("method") == "textDocument/publishDiagnostics"
        and item["params"]["uri"] == path.as_uri()
    ]
    return messages[-1]["diagnostics"] if messages else []


def test_confident_duplicates_and_literal_absence_refresh_and_clear(
    tmp_path, clients
):
    path = tmp_path / "main.sls"
    client = clients(tmp_path, settings=settings(tmp_path, stateRoots=["."]))
    client.open(
        path,
        "include:\n  - absent\nexample:\n  runtime.unknown: []\nexample:\n  test.nop: []\n",
    )
    findings = diagnostics(client, path)
    assert {item["code"] for item in findings} == {
        "duplicate-id",
        "unresolved-sls",
    }
    assert all(item["source"] == "salt-lsp" for item in findings)
    duplicate = next(
        item for item in findings if item["code"] == "duplicate-id"
    )
    assert duplicate["range"]["start"] == {"line": 4, "character": 0}
    (tmp_path / "absent.sls").write_text("other:\n  test.nop: []\n")
    client.notify(
        "workspace/didChangeWatchedFiles",
        {"changes": [{"uri": (tmp_path / "absent.sls").as_uri(), "type": 1}]},
    )
    assert [item["code"] for item in diagnostics(client, path)] == [
        "duplicate-id"
    ]
    change(client, path, "valid:\n  runtime.unknown: []\n")
    assert diagnostics(client, path) == []
    change(client, path, "include: invalid\n", version=3)
    assert diagnostics(client, path)[0]["code"] == "include-not-list"
    client.notify(
        "textDocument/didClose", {"textDocument": {"uri": path.as_uri()}}
    )
    assert diagnostics(client, path) == []


def test_generated_branches_unknown_functions_and_ambiguous_roles_are_not_errors(
    tmp_path, clients
):
    path = tmp_path / "main.sls"
    configuration = settings(tmp_path, stateRoots=["."])
    client = clients(tmp_path, settings=configuration)
    client.open(
        path,
        "{% if runtime %}\nchoice:\n  unknown.provider: []\n{% else %}\nchoice:\n  custom.conditional: []\n{% endif %}\ninclude:\n  - '{{ generated }}'\n",
    )
    assert diagnostics(client, path) == []
    change(client, path, "include:\n  - missing\n")
    assert diagnostics(client, path)[0]["code"] == "unresolved-sls"
    client.notify(
        "workspace/didChangeConfiguration",
        {
            "settings": {
                "salt": settings(tmp_path, stateRoots=["."], pillarRoots=["."])
            }
        },
    )
    assert diagnostics(client, path) == []
