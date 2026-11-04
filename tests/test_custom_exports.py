"""Cross-language exports at the standard LSP boundary."""

from test_projects import clients  # pylint: disable=unused-import
from test_state_call_assistance import complete, hover, labels, settings
from test_jinja_call_assistance import change


def definition(client, path, line, character):
    return client.request(
        "textDocument/definition",
        {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": character},
        },
    )


def test_virtual_function_alias_exports_navigate_to_live_python(
    tmp_path, clients
):
    directory = tmp_path / "_states"
    directory.mkdir()
    source = directory / "implementation.py"
    source.write_text(
        "__virtualname__ = 'acme'\n"
        "__func_alias__ = {'run_': 'run'}\n"
        "__load__ = ['run_']\n"
        "__all__ = ['helper']\n"
        "def __virtual__():\n    return __virtualname__\n"
        "def run_(name, *, mode='safe'):\n    '''Alias documentation.'''\n    pass\n"
        "def helper(): pass\n"
    )
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "x:\n  acme.run:\n    - mo\n")
    assert "mode" in labels(complete(client, path, 2, 8))
    assert (
        "Alias documentation" in hover(client, path, 1, 8)["contents"]["value"]
    )
    target = definition(client, path, 1, 8)
    assert target == [
        {
            "uri": source.as_uri(),
            "range": {
                "start": {"line": 6, "character": 4},
                "end": {"line": 6, "character": 8},
            },
        }
    ]
    client.open(
        source,
        "__virtualname__='acme'\ndef __virtual__(): return __virtualname__\n\ndef run(name): pass\n",
    )
    assert definition(client, path, 1, 8)[0]["range"]["start"]["line"] == 3
    change(client, source, "__load__=[]\ndef run(name): pass\n")
    assert definition(client, path, 1, 8) is None


def test_conflicting_execution_providers_keep_multiple_candidates(
    tmp_path, clients
):
    directory = tmp_path / "_modules"
    directory.mkdir()
    for filename, argument in (("one", "first"), ("two", "second")):
        (directory / f"{filename}.py").write_text(
            "__virtualname__='acme'\n"
            "def __virtual__():\n    return __virtualname__ if __grains__['x'] else False\n"
            f"def lookup({argument}): pass\n"
        )
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "{{ salt.acme.lookup() }}\n")
    targets = definition(client, path, 0, 15)
    assert {item["uri"] for item in targets} == {
        p.as_uri() for p in directory.glob("*.py")
    }
    text = hover(client, path, 0, 15)["contents"]["value"]
    assert (
        "2 conditional provider" in text
        and "first" in text
        and "second" in text
    )


def test_export_restrictions_bindings_and_virtual_metadata(tmp_path, clients):
    directory = tmp_path / "_states"
    directory.mkdir()
    source = directory / "implementation.py"
    source.write_text(
        "__virtualname__='ignored_without_hook'\n"
        "def original(name): pass\n"
        "alias = original\n"
        "def original(other): pass\n"
        "__func_alias__={'alias':'public', '_hidden':'leaked'}\n"
        "__all__=['alias', '_hidden']\n"
        "def _hidden(): pass\n"
    )
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "x:\n  implementation.p\n")
    assert labels(complete(client, path, 1, 18)) == {"public"}
    change(client, path, "x:\n  implementation.public:\n")
    assert definition(client, path, 1, 15)[0]["range"]["start"]["line"] == 1
    client.open(
        source,
        "__virtualname__ = 'acme'\n__virtual_aliases__=['also']\n"
        "def __virtual__(): return __virtualname__\n"
        "__load__=compute_exports()\n"
        "def run(name): pass\n",
    )
    change(client, path, "x:\n  also.run:\n")
    assert definition(client, path, 1, 7)[0]["uri"] == source.as_uri()


def test_empty_override_shadows_only_its_source_identity(tmp_path, clients):
    directory = tmp_path / "_states"
    directory.mkdir()
    source = directory / "file.py"
    source.write_text("# Empty authoritative provider\n")
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "x:\n  file.managed:\n")
    assert hover(client, path, 1, 8) is None
    source.unlink()
    assert (
        "source-known state API (salt/states/file.py:"
        in hover(client, path, 1, 8)["contents"]["value"]
    )
    (directory / "other.py").write_text(
        "__virtualname__='file'\ndef __virtual__(): return __virtualname__\n"
        "def managed(name): pass\n"
    )
    assert (
        "2 conditional provider"
        in hover(client, path, 1, 8)["contents"]["value"]
    )
