"""Function hover and argument completion through the real stdio LSP seam."""

import subprocess
import sys

from test_projects import clients  # pylint: disable=unused-import


def settings(root, version="3008"):
    return {
        "projects": [
            {
                "root": root.as_uri(),
                "profiles": {
                    "default": {"saltVersion": version, "stateRoots": ["."]}
                },
            }
        ]
    }


def hover(client, path, line, character):
    return client.request(
        "textDocument/hover",
        {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": character},
        },
    )


def complete(client, path, line, character):
    return client.request(
        "textDocument/completion",
        {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": character},
        },
    )


def test_arguments_replace_token_add_colon_and_carry_documentation(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "example:\n  file.managed:\n    - sou\n")

    items = complete(client, path, 2, 9)["items"]

    source = next(i for i in items if i["label"] == "source")
    assert {i["label"] for i in items} >= {"source", "source_hash"}
    assert all(i["label"].startswith("sou") for i in items)
    assert source["textEdit"] == {
        "range": {
            "start": {"line": 2, "character": 6},
            "end": {"line": 2, "character": 9},
        },
        "newText": "source: ",
    }
    assert "source=None" in source["detail"]
    assert "file.managed" in source["detail"]
    assert "Salt 3008.5" in source["detail"]
    assert source["documentation"].startswith(
        "The source file to download to the minion"
    )
    # The definition ends where the next argument's begins; examples keep shape.
    assert "a source hash string" not in source["documentation"]
    assert "\n    file_override_example:\n" in source["documentation"]


def test_existing_colon_and_value_survive_argument_replacement(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "example:\n  file.managed:\n    - sou: salt://x\n")

    items = complete(client, path, 2, 8)["items"]

    source = next(i for i in items if i["label"] == "source")
    assert source["textEdit"] == {
        "range": {
            "start": {"line": 2, "character": 6},
            "end": {"line": 2, "character": 9},
        },
        "newText": "source",
    }


def test_empty_entry_and_bare_dash_are_argument_positions(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "example:\n  file.managed:\n    - \n  cmd.run:\n  -\n")

    spaced = complete(client, path, 2, 6)["items"]
    required = spaced[0]
    assert required["label"] == "name"
    assert required["textEdit"] == {
        "range": {
            "start": {"line": 2, "character": 6},
            "end": {"line": 2, "character": 6},
        },
        "newText": "name: ",
    }
    assert "required" in required["detail"]
    ordered = sorted(spaced, key=lambda item: item["sortText"])
    assert ordered[0]["label"] == "name"
    assert [i["label"] for i in ordered[1:3]] == ["source", "source_hash"]

    bare = complete(client, path, 4, 3)["items"]
    name = next(i for i in bare if i["label"] == "name")
    assert name["textEdit"] == {
        "range": {
            "start": {"line": 4, "character": 3},
            "end": {"line": 4, "character": 3},
        },
        "newText": " name: ",
    }
    assert "cwd" in {i["label"] for i in bare}
    assert "source" not in {i["label"] for i in bare}


def labels(result):
    return {item["label"] for item in result["items"]}


def test_argument_documentation_styles_and_absent_documentation(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "example:\n"
        "  ansible.playbooks:\n    - \n"
        "  at.present:\n    - \n"
        "  certutil.add_store:\n    - \n"
        "  cmd.run:\n    - \n"
        "  apache.configfile:\n    - \n",
    )

    def documentation(line, name):
        items = complete(client, path, line, 6)["items"]
        return next(i for i in items if i["label"] == name).get(
            "documentation"
        )

    assert documentation(2, "name").startswith("path to playbook")
    assert documentation(2, "rundir") == (
        "location to run ansible-playbook from."
    )
    assert documentation(4, "job").startswith("string\n")
    assert "Command to run." in documentation(4, "job")
    assert documentation(6, "name").startswith(
        "The path to the certificate to add"
    )
    assert documentation(8, "success_stdout").startswith(
        "This parameter will allow a list of"
    )
    # The catalog's docstring is empty: the name is still offered, undocumented.
    assert documentation(10, "config") is None


def test_only_nameable_arguments_are_offered(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path, "3006"))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "example:\n"
        "  module.run:\n    - \n"
        "  pip.purge_pip:\n    - \n"
        "  btrfs.properties:\n    - \n",
    )

    assert complete(client, path, 2, 6)["items"] == []
    assert complete(client, path, 4, 6)["items"] == []
    properties = labels(complete(client, path, 6, 6))
    # properties(name, device, use_default=False, __dest=None, **properties)
    assert properties == {"name", "device", "use_default"}


def test_specified_arguments_are_hidden_except_the_one_being_edited(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "example:\n"
        "  file.managed:\n"
        "    - name: /tmp/example\n"
        "    - source:\n"
        "      - salt://a\n"
        "    - mo\n"
        "    - user: root\n"
        "    - group: root\n"
        "other:\n"
        "  file.managed:\n"
        "    - mo\n",
    )

    offered = labels(complete(client, path, 5, 6))
    assert not offered & {"name", "source", "user", "group"}
    assert {"makedirs", "mode"} <= offered

    editing = labels(complete(client, path, 6, 6))
    assert not editing & {"name", "source", "group"}
    # Another state's arguments have no bearing on this one.
    assert {"name", "source", "user"} <= labels(complete(client, path, 10, 6))


def test_nested_data_gets_no_argument_suggestions(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    negatives = [
        ("mapping", "    - context:\n        so\n", 3, 10),
        ("mapping list", "    - context:\n        - so\n", 3, 12),
        ("list value", "    - source:\n      - so\n", 3, 10),
        ("requisite", "    - require:\n      - \n", 3, 8),
        (
            "requisite same dash",
            "    - watch:\n      - file: a\n      - \n",
            4,
            8,
        ),
        ("block scalar", "    - contents: |\n        - so\n", 3, 12),
        (
            "state in scalar",
            "    - contents: |\n        a:\n          file.managed:\n"
            "            - so\n",
            5,
            16,
        ),
        ("second key", "    - name: a\n      so\n", 3, 8),
        ("value", "    - name: so\n", 2, 14),
        ("comment", "    # - so\n", 2, 8),
        ("no dash", "    so\n", 2, 6),
        ("flow", "    - {so\n", 2, 9),
        ("scalar without dash space", "    -so\n", 2, 7),
    ]
    for index, (label, body, line, character) in enumerate(negatives):
        path = tmp_path / f"negative{index}.sls"
        client.open(path, "example:\n  file.managed:\n" + body)
        result = complete(client, path, line, character)
        assert not result or not any(
            i.get("data", {}).get("kind") == "state-argument"
            for i in result["items"]
        ), label
    for name, text, line, character in [
        ("top", "- so\n", 0, 4),
        ("under id", "example:\n  - so\n", 1, 6),
        ("include", "include:\n  - so\n", 1, 6),
        ("nested", "a:\n  b:\n    file.managed:\n      - so\n", 3, 8),
        ("extend key", "extend:\n  file.managed:\n    - so\n", 2, 8),
    ]:
        path = tmp_path / f"{name}.sls"
        client.open(path, text)
        result = complete(client, path, line, character)
        assert not result or not any(
            i.get("data", {}).get("kind") == "state-argument"
            for i in result["items"]
        ), name


def test_all_literal_call_forms_are_recognized(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "indentless:\n"  # 0
        "  file.managed:\n"
        "  - name: a\n"
        "  - sou\n"
        "module_key:\n"  # 4
        "  file:\n"
        "    - managed\n"
        "    - sou\n"
        "extend:\n"  # 8
        "  base:\n"
        "    file.managed:\n"
        "      - sou\n"
        "unfinished_key:\n"  # 12
        "  file.managed\n"
        "    - sou\n"
        "😀: file.managed\n",  # 15
    )
    for line, character in ((3, 7), (7, 9), (11, 11), (14, 9)):
        assert "source" in labels(complete(client, path, line, character))
    assert "name" not in labels(complete(client, path, 3, 4))

    for line, character, start, end in (
        (1, 3, 2, 14),
        (5, 3, 2, 6),  # the module key, resolved through its list
        (6, 8, 6, 13),  # the function scalar
        (10, 6, 4, 16),
        (13, 3, 2, 14),
        # Columns count UTF-16 units after the astral state ID.
        (15, 5, 4, 16),
    ):
        result = hover(client, path, line, character)
        assert result["contents"]["value"].startswith("file.managed("), line
        assert result["range"] == {
            "start": {"line": line, "character": start},
            "end": {"line": line, "character": end},
        }


def test_line_endings_do_not_change_assistance(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    for index, ending in enumerate(("\r\n", "\r")):
        path = tmp_path / f"a{index}.sls"
        client.open(
            path,
            ending.join(
                ("example:", "  file.managed:", "    - sou", "    -", "")
            ),
        )
        assert hover(client, path, 1, 4)["range"]["end"]["character"] == 14
        source = next(
            i
            for i in complete(client, path, 2, 9)["items"]
            if i["label"] == "source"
        )
        assert source["textEdit"]["newText"] == "source: "
        assert source["textEdit"]["range"]["end"]["character"] == 9
        assert "name" in labels(complete(client, path, 3, 5))


def test_module_key_without_one_function_stays_unknown(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "none:\n  file:\n    - name: a\n    - so\n"
        "two:\n  file:\n    - managed\n    - absent\n    - so\n",
    )
    assert complete(client, path, 3, 8) is None
    assert complete(client, path, 8, 8) is None
    assert hover(client, path, 1, 3) is None
    assert hover(client, path, 5, 3) is None
    assert hover(client, path, 6, 8) is None


def test_template_syntax_is_lexed_not_evaluated(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    text = (
        "{% set files = ['a'] %}\n"  # 0
        "{% for f in files %}\n"
        "{{ f }}_config:\n"
        "  file.managed:\n"
        "{% if x %}\n"  # 4
        "    - name: {{ f }}\n"
        "{% else %}\n"
        "    - user: {{ x }}\n"
        "{% endif %}\n"
        "    - sou\n"  # 9
        "    - {{ dynamic }}: 1\n"
        "{% endfor %}\n"
        "{{ module }}.managed:\n"  # 12
        "  file.{{ fn }}:\n"
        "    - sou\n"
        "  {{ module }}.managed:\n"  # 15
        "    - sou\n"
        "{% set multi = [\n"  # 17
        "  'a',\n"
        "] %}\n"
        "{% load_yaml as data %}\n"  # 20
        "captured:\n"
        "  file.managed:\n"
        "    - sou\n"
        "{% endload %}\n"
        "{% set block %}\n"  # 25
        "captured_set:\n"
        "  file.managed:\n"
        "    - sou\n"
        "{% endset %}\n"
        "{# a:\n"  # 30
        "  file.managed:\n"
        "    - sou\n"
        "#}\n"
        "raw:\n"  # 34
        "  file.managed:\n"
        "    - {% raw %}\n"
        "    - {% if\n"
    )
    client.open(path, text)

    offered = labels(complete(client, path, 9, 9))
    assert "source" in offered
    assert not offered & {"name", "user"}
    assert hover(client, path, 3, 5)["contents"]["value"].startswith(
        "file.managed("
    )
    for line, character in (
        (14, 9),
        (16, 9),
        (23, 9),
        (28, 9),
        (32, 9),
        (36, 8),
        (37, 8),
    ):
        result = complete(client, path, line, character)
        assert not result or not any(
            i.get("data", {}).get("kind") == "state-argument"
            for i in result["items"]
        ), line
    for line, character in (
        (12, 15),
        (13, 4),
        (15, 15),
        (22, 5),
        (27, 5),
        (31, 5),
    ):
        assert hover(client, path, line, character) is None, line


def test_incomplete_template_above_leaves_later_states_usable(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    for index, opening in enumerate(
        (
            "{% if unfinished\n",
            "{{ unfinished\n",
            "{# unfinished\n",
            "{% set text = 'unfinished\n",
            "{% load_yaml as unfinished %}\n",
        )
    ):
        path = tmp_path / f"a{index}.sls"
        client.open(
            path,
            opening + "example:\n  file.managed:\n    - sou\n{% if x %}\n",
        )
        assert "source" in labels(complete(client, path, 3, 9)), opening
        assert hover(client, path, 2, 4) is not None, opening


def test_unknown_functions_and_unusable_positions_degrade_quietly(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "example:\n"
        "  nosuch.function:\n    - na\n"
        "  file.nosuch:\n    - na\n"
        "  docker_container.running:\n    - na\n"
        "  file.managed:\n    - name: file.managed\n"
        "file.managed:\n"
        "include: file.managed\n"
        "# file.managed:\n",
    )
    for line in (1, 3, 5):
        assert hover(client, path, line, 4) is None
        assert complete(client, path, line + 1, 8) is None
    for line, character in (
        (0, 2),  # state ID
        (7, 15),  # the colon after the call
        (8, 8),  # argument name
        (8, 20),  # value that names a function
        (9, 3),  # a state ID that looks like a call
        (10, 12),  # reserved key
        (11, 5),  # comment
        (500, 0),  # past the end of the document
    ):
        assert hover(client, path, line, character) is None, (line, character)
    assert complete(client, path, 500, 0) is None
    # A very long column is clamped, not a failure.
    assert hover(client, path, 1, 10_000) is None

    empty = tmp_path / "empty.sls"
    client.open(empty, "")
    assert hover(client, empty, 0, 0) is None
    assert complete(client, empty, 0, 0) is None

    unopened = tmp_path / "unopened.sls"
    assert hover(client, unopened, 0, 0) is None
    assert (
        client.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": "untitled:scratch"},
                "position": {"line": 0, "character": 0},
            },
        )
        is None
    )


def test_hover_without_catalog_documentation_is_still_useful(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "example:\n  apache.configfile:\n    - name: /etc/a\n")

    text = hover(client, path, 1, 5)["contents"]["value"]

    assert text.startswith("apache.configfile(name, config)")
    assert "Salt 3008.5" in text
    assert "no documentation" in text


def test_aliases_and_conditional_providers_keep_provenance(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(
        path,
        "example:\n"
        "  cmd.watch:\n    - name: a\n"
        "  x509.certificate_managed:\n    - \n",
    )

    alias = hover(client, path, 1, 4)["contents"]["value"]
    assert "Alias of wait" in alias

    both = hover(client, path, 3, 8)["contents"]["value"]
    assert "2 conditional provider candidates" in both
    assert "salt/states/x509.py" in both
    assert "salt/states/x509_v2.py" in both

    items = {i["label"]: i for i in complete(client, path, 4, 6)["items"]}
    assert set(items["days_remaining"]["data"]["providers"]) == {
        "salt/states/x509.py",
        "salt/states/x509_v2.py",
    }
    assert items["ca_server"]["data"]["providers"] == [
        "salt/states/x509_v2.py"
    ]
    assert "[salt/states/x509_v2.py]" in items["ca_server"]["detail"]
    assert "[salt/states/x509.py]" not in items["ca_server"]["detail"]
    assert "salt/states/x509_v2.py:\n" in items["ca_server"]["documentation"]


def test_live_profile_switch_changes_hover_and_arguments(tmp_path, clients):
    projects = [
        {
            "root": tmp_path.as_uri(),
            "activeProfile": "3006",
            "profiles": {
                v: {"saltVersion": v, "stateRoots": ["."]}
                for v in ("3006", "3007", "3008")
            },
        }
    ]
    client = clients(tmp_path, settings={"projects": projects})
    path = tmp_path / "a.sls"
    client.open(
        path,
        "example:\n"
        "  test.configurable_test_state:\n    - \n"
        "  docker_container.running:\n    - \n",
    )
    releases = {"3006": "3006.30", "3007": "3007.17", "3008": "3008.5"}
    for version in ("3006", "3007", "3008", "3006"):
        projects[0]["activeProfile"] = version
        client.notify(
            "workspace/didChangeConfiguration",
            {"settings": {"salt": {"projects": projects}}},
        )
        text = hover(client, path, 1, 8)["contents"]["value"]
        assert f"Salt {releases[version]}" in text
        assert ("allow_test_mode_failure=False" in text) == (version != "3006")

        result = complete(client, path, 2, 6)
        assert ("allow_test_mode_failure" in labels(result)) == (
            version != "3006"
        )
        assert all(i["data"]["version"] == version for i in result["items"])
        assert all(releases[version] in i["detail"] for i in result["items"])

        # Absent from 3008's catalog: unknown there, not an error or a guess.
        docker_hover = hover(client, path, 3, 8)
        docker_arguments = complete(client, path, 4, 6)
        if version == "3008":
            assert docker_hover is None
            assert docker_arguments is None
        else:
            assert f"Salt {releases[version]}" in (
                docker_hover["contents"]["value"]
            )
            assert "image" in labels(docker_arguments)


def test_non_state_roles_get_no_call_assistance(tmp_path, clients):
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": tmp_path.as_uri(),
                    "profiles": {
                        "default": {
                            "stateRoots": ["states"],
                            "pillarRoots": ["pillar"],
                        }
                    },
                }
            ]
        },
    )
    text = "example:\n  file.managed:\n    - so\n"
    for directory in ("pillar", "generic"):
        path = tmp_path / directory / "a.sls"
        client.open(path, text)
        assert hover(client, path, 1, 4) is None
        assert complete(client, path, 2, 8) is None
    top = tmp_path / "states" / "top.sls"
    client.open(top, text)
    assert hover(client, top, 1, 4) is None


def test_assistance_never_imports_salt_or_workspace_code(
    tmp_path, clients, monkeypatch
):
    marker = tmp_path / "imported"
    poison = tmp_path / "poison"
    (poison / "salt").mkdir(parents=True)
    trap = f"open({str(marker)!r}, 'w').close()\n"
    (poison / "salt" / "__init__.py").write_text(trap)
    for module in ("file", "managed"):
        (poison / f"{module}.py").write_text(trap)
    # The trap really fires for anything that imports through this path.
    monkeypatch.setenv("PYTHONPATH", str(poison))
    subprocess.run([sys.executable, "-c", "import salt"], check=True)
    assert marker.exists()
    marker.unlink()

    client = clients(tmp_path, settings=settings(tmp_path))
    states = tmp_path / "_states"
    states.mkdir()
    (states / "file.py").write_text(trap + "def managed(name): pass\n")
    path = tmp_path / "a.sls"
    client.open(path, "{{ salt['cmd.run']('x') }}:\n  file.managed:\n    - \n")

    assert hover(client, path, 1, 4) is not None
    assert "name" in labels(complete(client, path, 2, 6))
    assert not marker.exists()


def test_unparseable_template_edit_does_not_keep_stale_analysis(
    tmp_path, clients
):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "old:\n  file.managed:\n    - name: a\n")
    assert [s["name"] for s in client.symbols(path)] == ["old"]

    client.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": path.as_uri(), "version": 2},
            "contentChanges": [
                {"text": "{{ generated }}:\n  file.managed:\n    - \n"}
            ],
        },
    )

    assert "old" not in [s["name"] for s in client.symbols(path)]
    assert "name" in labels(complete(client, path, 2, 6))
    assert hover(client, path, 1, 4) is not None


def test_hover_documents_function_with_version_and_provider(tmp_path, clients):
    client = clients(tmp_path, settings=settings(tmp_path))
    path = tmp_path / "a.sls"
    client.open(path, "example:\n  file.managed:\n    - name: /tmp/example\n")

    result = hover(client, path, 1, 8)

    assert result["range"] == {
        "start": {"line": 1, "character": 2},
        "end": {"line": 1, "character": 14},
    }
    text = result["contents"]["value"]
    assert result["contents"]["kind"] == "plaintext"
    assert text.startswith("file.managed(name, source=None")
    assert "Salt 3008.5" in text
    assert "salt/states/file.py" in text
    assert "not proof of provider availability" in text
    assert "Manage a given file" in text
