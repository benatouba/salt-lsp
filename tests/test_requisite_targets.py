"""Requisite navigation resolves candidates, never an arbitrary global ID."""

from test_projects import clients  # pylint: disable=unused-import
from test_source_references import settings
from test_custom_exports import definition


def test_names_modules_wildcards_and_ambiguous_include_candidates(
    tmp_path, clients
):
    (tmp_path / "dep.sls").write_text(
        "same:\n  file.managed:\n    - name: /etc/example\nother:\n  service.running: []\n"
    )
    (tmp_path / "unrelated.sls").write_text("same:\n  file.managed: []\n")
    path = tmp_path / "main.sls"
    text = "include:\n  - dep\nsame:\n  pkg.installed: []\ncheck:\n  test.nop:\n    - require_any:\n      - file: /etc/example\n      - same\n      - file: same\n      - service: same\n      - sls: dep\n      - file: '*'\n"
    client = clients(tmp_path, settings=settings(tmp_path, stateRoots=["."]))
    client.open(path, text)
    target = definition(client, path, 7, 18)
    assert target["uri"] == (tmp_path / "dep.sls").as_uri()
    ambiguous = definition(client, path, 8, 10)
    assert {item["uri"] for item in ambiguous} == {
        path.as_uri(),
        (tmp_path / "dep.sls").as_uri(),
    }
    assert (
        definition(client, path, 9, 16)["uri"]
        == (tmp_path / "dep.sls").as_uri()
    )
    assert definition(client, path, 10, 19) is None
    assert len(definition(client, path, 11, 15)) == 2
    assert (
        definition(client, path, 12, 16)["uri"]
        == (tmp_path / "dep.sls").as_uri()
    )


def test_conditional_duplicates_names_and_dynamic_targets_are_candidates(
    tmp_path, clients
):
    path = tmp_path / "main.sls"
    text = "{% if condition %}\nchoice:\n  file.managed:\n    - names:\n      - /etc/a\n{% else %}\nchoice:\n  file.managed: []\n{% endif %}\ncheck:\n  test.nop:\n    - onfail_all:\n      - file: choice\n      - file: /etc/a\n      - file: '{{ generated }}'\n"
    client = clients(tmp_path, settings=settings(tmp_path, stateRoots=["."]))
    client.open(path, text)
    result = definition(client, path, 12, 17)
    assert [item["range"]["start"]["line"] for item in result] == [1, 6]
    assert definition(client, path, 13, 17)["range"]["start"]["line"] == 1
    assert definition(client, path, 14, 20) is None
