"""Literal source references through real LSP requests."""

from test_projects import clients  # pylint: disable=unused-import
from test_state_call_assistance import complete, labels
from test_custom_exports import definition
from test_jinja_call_assistance import change


def settings(root, **profile):
    return {
        "projects": [{"root": str(root), "profiles": {"default": profile}}]
    }


def test_reference_roots_relative_environment_templates_and_freshness(
    tmp_path, clients
):
    states = tmp_path / "states"
    states.mkdir()
    formula = states / "formula"
    formula.mkdir()
    target = formula / "init.sls"
    target.write_text("include:\n  - formula\n")
    template = states / "map.jinja"
    template.write_text("{% set data = {} %}\n")
    (states / "top.sls").write_text("base: {}\n")
    (states / "init.sls").write_text("")
    client = clients(
        tmp_path, settings=settings(tmp_path, stateRoots=["states"])
    )
    path = formula / "main.sls"
    client.open(
        path,
        "include:\n  - .init\n  - base: formula\n{% from 'map.jinja' import data %}\n",
    )
    assert definition(client, path, 1, 6)[0]["uri"] == target.as_uri()
    assert definition(client, path, 2, 15)[0]["uri"] == target.as_uri()
    assert definition(client, path, 3, 15)[0]["uri"] == template.as_uri()
    change(client, path, "include:\n  - \n")
    assert labels(complete(client, path, 1, 4)) == {"formula"}
    target.unlink()
    assert "formula" not in labels(complete(client, path, 1, 4))
    client.open(target, "# authoritative unsaved source\n")
    assert "formula" in labels(complete(client, path, 1, 4))


def test_pillar_boundaries_and_symlink_escape(tmp_path, clients):
    states = tmp_path / "states"
    pillars = tmp_path / "pillar"
    states.mkdir()
    pillars.mkdir()
    (states / "state.sls").write_text("")
    target = pillars / "data.sls"
    target.write_text("")
    outside = tmp_path / "escape.sls"
    outside.write_text("")
    (pillars / "escape.sls").symlink_to(outside)
    client = clients(
        tmp_path,
        settings=settings(
            tmp_path, stateRoots=["states"], pillarRoots=["pillar"]
        ),
    )
    path = pillars / "main.sls"
    client.open(path, "include:\n  - data\n  - escape\n")
    assert definition(client, path, 1, 7)[0]["uri"] == target.as_uri()
    assert definition(client, path, 2, 7) is None
    change(client, path, "include:\n  - \n")
    assert labels(complete(client, path, 1, 4)) == {"data"}


def test_ordered_roots_cycles_and_disk_dependency_changes(tmp_path, clients):
    for root in ("first", "second"):
        (tmp_path / root).mkdir()
        (tmp_path / root / "target.sls").write_text(
            "old:\n  file.managed: []\ninclude:\n  - main\n"
        )
    client = clients(
        tmp_path, settings=settings(tmp_path, stateRoots=["first", "second"])
    )
    path = tmp_path / "first/main.sls"
    text = "include:\n  - target\n  - main\ncheck:\n  file.managed:\n    - require:\n      - file: old\n"
    client.open(path, text)
    assert (
        definition(client, path, 1, 7)[0]["uri"]
        == (tmp_path / "first/target.sls").as_uri()
    )
    assert (
        definition(client, path, 6, 16)["uri"]
        == (tmp_path / "first/target.sls").as_uri()
    )
    (tmp_path / "first/target.sls").write_text(
        "new:\n  file.managed: []\ninclude:\n  - main\n"
    )
    assert definition(client, path, 6, 16) is None
    (tmp_path / "first/target.sls").unlink()
    assert (
        definition(client, path, 6, 16)["uri"]
        == (tmp_path / "second/target.sls").as_uri()
    )
    change(
        client,
        path,
        "{% include 'target.sls' ~ unknown %}\ninclude:\n  - other: target\n",
    )
    assert definition(client, path, 0, 16) is None
    assert definition(client, path, 2, 15) is None


def test_special_files_are_not_read_as_include_sources(tmp_path, clients):
    import os

    os.mkfifo(tmp_path / "blocked.sls")
    client = clients(tmp_path, settings=settings(tmp_path, stateRoots=["."]))
    path = tmp_path / "a.sls"
    client.open(path, "include:\n  - blocked\n")
    result = client.request(
        "textDocument/definition",
        {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": 1, "character": 5},
        },
    )
    assert not result
    assert client.symbols(path)
