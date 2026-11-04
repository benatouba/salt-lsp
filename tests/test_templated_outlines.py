"""Editor outlines recover independent declarations without rendering."""

from test_projects import clients  # pylint: disable=unused-import
from test_source_references import settings
from test_jinja_call_assistance import change


def test_mixed_template_outline_recovers_after_malformed_stanza(
    tmp_path, clients
):
    path = tmp_path / "main.sls"
    text = "{% if condition %}\n'é😀':\n  file.managed:\n    - name: {{ dynamic }}\n{% endif %}\nbroken:\n  file.managed:\n    - name: \"unclosed\nafter:\n  test.nop: []\ninclude:\n  - dep\nextend:\n  old:\n    file.managed:\n      - mode: '0644'\n"
    client = clients(tmp_path, settings=settings(tmp_path, stateRoots=["."]))
    client.open(path, text)
    symbols = client.symbols(path)
    by_name = {symbol["name"]: symbol for symbol in symbols}
    assert {"é😀", "after", "includes", "extend"} <= by_name.keys()
    assert by_name["é😀"]["selectionRange"] == {
        "start": {"line": 1, "character": 1},
        "end": {"line": 1, "character": 4},
    }
    parameter = by_name["é😀"]["children"][0]["children"][0]
    assert parameter["name"] == "name"
    assert parameter["selectionRange"]["start"] == {"line": 3, "character": 6}
    assert by_name["after"]["range"]["start"]["line"] == 8
    change(client, path, "new:\n  test.nop: []\n")
    assert [item["name"] for item in client.symbols(path)] == ["new"]


def test_client_without_hierarchical_symbols_gets_flat_locations(
    tmp_path, clients
):
    path = tmp_path / "main.sls"
    client = clients(
        tmp_path,
        settings=settings(tmp_path, stateRoots=["."]),
        capabilities={},
    )
    client.open(path, "example:\n  file.managed:\n    - name: /tmp/example\n")
    symbols = client.symbols(path)
    assert [symbol["name"] for symbol in symbols] == [
        "example",
        "file.managed",
        "name",
    ]
    assert all(
        symbol["location"]["uri"] == path.as_uri() and "children" not in symbol
        for symbol in symbols
    )
    assert symbols[1]["containerName"] == "example"
