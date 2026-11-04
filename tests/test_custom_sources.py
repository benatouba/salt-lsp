"""Live project APIs through standard LSP, never Python imports."""

from test_projects import clients  # pylint: disable=unused-import
from test_state_call_assistance import complete, hover, labels, settings
from test_jinja_call_assistance import change


def test_live_custom_state_and_execution_sources(tmp_path, clients):
    states = tmp_path / "_states"
    modules = tmp_path / "_modules"
    states.mkdir()
    modules.mkdir()
    source = states / "acme.py"
    sentinel = tmp_path / "EXECUTED"
    source.write_text(
        f"open({str(sentinel)!r}, 'w').write('bad')\n"
        "def present(name, mode='safe', *, timeout=probe()):\n"
        "    '''Project state documentation.'''\n"
        "    pass\n"
        "class Hidden:\n    def method(self): pass\n"
    )
    (modules / "acme.py").write_text(
        "def lookup(key, fallback=None):\n    '''Project execution docs.'''\n    pass\n"
    )
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "x:\n  acme.present:\n    - mo\n{{ salt.acme.lookup('x', fa) }}\n",
    )
    assert "mode" in labels(complete(client, path, 2, 8))
    assert (
        "Project state documentation"
        in hover(client, path, 1, 8)["contents"]["value"]
    )
    assert "fallback" in labels(complete(client, path, 3, 25))
    client.open(
        source,
        "def updated(name, fresh=True):\n    '''Unsaved docs.'''\n    pass\n",
    )
    change(client, path, "x:\n  acme.\n")
    assert labels(complete(client, path, 1, 7)) == {"updated"}
    change(client, source, "def broken(")
    assert not labels(complete(client, path, 1, 7))
    change(client, source, "def repaired(name): pass\n", version=3)
    assert labels(complete(client, path, 1, 7)) == {"repaired"}
    client.notify(
        "textDocument/didClose", {"textDocument": {"uri": source.as_uri()}}
    )
    assert labels(complete(client, path, 1, 7)) == {"present"}
    source.unlink()
    assert not labels(complete(client, path, 1, 7))
    assert not sentinel.exists()


def test_profile_roots_and_additional_execution_directories(tmp_path, clients):
    for root, argument in (("a", "first"), ("b", "second")):
        directory = tmp_path / root / "_states"
        directory.mkdir(parents=True)
        (directory / "acme.py").write_text(
            f"def present(name, {argument}=True): pass\n"
        )
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "lookup.py").write_text("def get(value): pass\n")
    configuration = {
        "salt": {
            "projects": [
                {
                    "root": str(tmp_path),
                    "activeProfile": "a",
                    "profiles": {
                        "a": {
                            "stateRoots": ["a", "b"],
                            "moduleDirs": ["extra"],
                        },
                        "b": {"stateRoots": ["b", "a"]},
                    },
                }
            ]
        }
    }
    client = clients(tmp_path, settings=configuration["salt"])
    path = tmp_path / "a" / "test.sls"
    client.open(path, "x:\n  acme.present:\n    - \n{{ salt.lookup. }}\n")
    assert "first" in labels(complete(client, path, 2, 6))
    assert "second" not in labels(complete(client, path, 2, 6))
    assert "get" in labels(complete(client, path, 3, 15))
    configuration["salt"]["projects"][0]["activeProfile"] = "b"
    client.notify(
        "workspace/didChangeConfiguration", {"settings": configuration}
    )
    assert "second" in labels(complete(client, path, 2, 6))
    assert "first" not in labels(complete(client, path, 2, 6))
    assert "get" not in labels(complete(client, path, 3, 15))
