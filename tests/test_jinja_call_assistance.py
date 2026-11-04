"""Execution-call assistance inside Jinja through the real stdio LSP seam."""

from test_projects import clients  # pylint: disable=unused-import
from test_state_call_assistance import (  # pylint: disable=unused-import
    complete,
    hover,
    labels,
    settings,
)


def change(client, path, text, version=2):
    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": version},
            "contentChanges": [{"text": text}],
        },
    )


def edit(item):
    return item["textEdit"]["newText"], item["textEdit"]["range"]


def test_bracket_function_completion_and_hover(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = "x:\n  cmd.run:\n    - name: {{ salt['cmd.ru'](\"ls\") }}\n"
    client.open(path, text)
    column = text.splitlines()[2].index("cmd.ru") + 6

    result = complete(client, path, 2, column)
    item = next(i for i in result["items"] if i["label"] == "cmd.run")
    assert all(i["label"].startswith("cmd.ru") for i in result["items"])
    new_text, edit_range = edit(item)
    assert new_text == "cmd.run"
    assert (
        edit_range["end"]["character"] - edit_range["start"]["character"] == 6
    )
    assert item["data"]["kind"] == "execution"
    assert "Salt 3008.5" in item["detail"]

    # Hover documents the complete name; the unfinished one stays unknown.
    assert hover(client, path, 2, column) is None
    change(client, path, text.replace("cmd.ru'", "cmd.run'"))
    value = hover(client, path, 2, column)["contents"]["value"]
    assert value.startswith("cmd.run(")
    assert "execution API" in value
    assert "salt/modules/cmdmod.py" in value
    assert "not proof of provider availability" in value


def test_attribute_forms_complete_modules_and_functions(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = "{{ salt.cm }}\n{{ salt.cmd.ru }}\n{{ salt.cmd. }}\n{{ salt. }}\n"
    client.open(path, text)

    modules = complete(client, path, 0, 10)
    assert "cmd" in labels(modules)
    assert all(i["label"].startswith("cm") for i in modules["items"])
    assert edit(next(i for i in modules["items"] if i["label"] == "cmd"))[
        0
    ] == ("cmd.")
    functions = complete(client, path, 1, 14)
    assert "run" in labels(functions)
    assert all(i["label"].startswith("ru") for i in functions["items"])
    assert "run" in labels(complete(client, path, 2, 12))
    assert {"cmd", "file"} <= labels(complete(client, path, 3, 8))

    change(client, path, "{{ salt.cmd.run('x') }}\n")
    assert hover(client, path, 0, 14)["contents"]["value"].startswith(
        "cmd.run("
    )


def test_keyword_arguments_complete_in_the_call_only(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = (
        "{{ salt['cmd.run']('ls', cwd='/', ru) }}\n"  # 0
        "{{ salt.cmd.run(\n"  # 1
        "     ) }}\n"
        "{{ salt['cmd.run']('ru', foo(ru), [ru]) }}\n"  # 3
        "{{ salt['cmd.run'](cwd='x') }} ru\n"  # 4
    )
    client.open(path, text)

    first = complete(client, path, 0, text.splitlines()[0].index("ru)") + 2)
    assert all(i["label"].startswith("ru") for i in first["items"])
    assert all(
        i["data"]["kind"] == "execution-argument" for i in first["items"]
    )
    runas = next(i for i in first["items"] if i["label"] == "runas")
    assert edit(runas)[0] == "runas="
    assert "cmd.run" in runas["detail"]

    hidden = complete(client, path, 0, text.splitlines()[0].index(", ru") + 2)
    assert hidden is not None and "cwd" not in labels(hidden)

    empty = complete(client, path, 1, 16)
    assert "cwd" in labels(empty)
    assert "runas" in labels(empty)
    for line, needle in ((3, "'ru"), (3, "(ru)"), (3, "[ru"), (4, " ru")):
        column = text.splitlines()[line].index(needle) + len(needle)
        result = complete(client, path, line, column)
        assert not result or not any(
            i["data"]["kind"] == "execution-argument" for i in result["items"]
        ), (line, needle)


def test_existing_equals_is_not_duplicated(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = "{{ salt['cmd.run']('ls', cw='/') }}\n"
    client.open(path, text)

    items = complete(client, path, 0, text.index("cw") + 2)["items"]
    assert edit(next(i for i in items if i["label"] == "cwd"))[0] == "cwd"


def test_control_blocks_comments_raw_and_unknowns(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = (
        "{% set out = salt['cmd.run']('x') %}\n"  # 0
        "{# salt['cmd.run']('x') #}\n"  # 1
        "{% raw %}{{ salt['cmd.run']('x') }}{% endraw %}\n"  # 2
        "{{ salt[name]('x') }}\n"  # 3
        "{{ salt['cmd.' ~ fn]('x') }}\n"  # 4
        "{{ salt['no.such']('x') }}\n"  # 5
        "{{ foo.salt.cmd.run('x') }}\n"  # 6
        "{{ salt['cmd.run'](\n"  # 7
        "x }}\n"
    )
    client.open(path, text)

    assert hover(client, path, 0, 24)["contents"]["value"].startswith(
        "cmd.run("
    )
    for line, character in (
        (1, 14),
        (2, 28),
        (3, 8),
        (4, 14),
        (5, 14),
        (6, 18),
    ):
        assert hover(client, path, line, character) is None, line
    assert hover(client, path, 8, 0) is None


def test_incomplete_templates_do_not_disable_state_assistance(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = (
        "{{ salt['cmd.run'](\n"
        "{% if\n"
        "x:\n"
        "  file.managed:\n"
        "    - sou\n"
    )
    client.open(path, text)

    assert "source" in labels(complete(client, path, 4, 9))
    assert hover(client, path, 3, 5)["contents"]["value"].startswith(
        "file.managed("
    )
    assert complete(client, path, 0, 19) is not None
    assert hover(client, path, 99, 0) is None


def test_unicode_positions_and_crlf(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = "# 😀 ü\r\nx: {{ '😀' }} {{ salt['cmd.run']('x') }}\r\n"
    client.open(path, text)

    line = text.split("\r\n")[1]
    prefix = line[: line.index("cmd.run") + 3]
    column = len(prefix.encode("utf-16-le")) // 2
    result = hover(client, path, 1, column)
    assert result["contents"]["value"].startswith("cmd.run(")
    start = len(line[: line.index("cmd.run")].encode("utf-16-le")) // 2
    assert result["range"] == {
        "start": {"line": 1, "character": start},
        "end": {"line": 1, "character": start + 7},
    }
    items = complete(client, path, 1, column)["items"]
    assert items[0]["textEdit"]["range"]["start"]["character"] == start


def test_profile_switch_selects_the_execution_catalog(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path, "3006"))
    path = tmp_path / "a.sls"
    client.open(path, "{{ salt['cmd.run']('x') }}\n")
    assert "Salt 3006.30" in hover(client, path, 0, 12)["contents"]["value"]
    client.notify(
        "workspace/didChangeConfiguration",
        {"settings": {"salt": settings(tmp_path, "3008")}},
    )
    assert "Salt 3008.5" in hover(client, path, 0, 12)["contents"]["value"]


def test_non_state_roles_get_no_template_assistance(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "top.sls"
    client.open(path, "{{ salt['cmd.run']('x') }}\n")
    assert hover(client, path, 0, 12) is None


def test_catalog_membership_is_distinct_from_availability(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "{{ salt['pkg.install']('x') }}\n")
    value = hover(client, path, 0, 12)["contents"]["value"]
    assert "conditional provider candidates" in value
    assert "salt/modules/aptpkg.py" in value


def test_template_assistance_never_imports_salt_or_workspace_code(
    tmp_path, clients
):
    marker = tmp_path / "imported"
    trap = f"open({str(marker)!r}, 'w').close()\n"
    client = clients(tmp_path, settings=settings(tmp_path))
    for folder in ("_modules", "_states"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "cmd.py").write_text(trap)
    (tmp_path / "salt.py").write_text(trap)
    path = tmp_path / "a.sls"
    client.open(path, "{{ salt['cmd.run']('x', cw='/') }}\n")

    assert hover(client, path, 0, 12) is not None
    assert "cwd" in labels(complete(client, path, 0, 26))
    assert not marker.exists()
