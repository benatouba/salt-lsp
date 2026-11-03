"""Offline state authoring through the real stdio LSP seam."""

from test_projects import clients  # pylint: disable=unused-import


def complete(client, path, line=1, character=7):
    return client.request(
        "textDocument/completion",
        {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": character},
        },
    )


def test_offline_function_documentation_and_replacement(tmp_path, clients):
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": tmp_path.as_uri(),
                    "profiles": {"default": {"stateRoots": ["."]}},
                }
            ]
        },
    )
    path = tmp_path / "a.sls"
    client.open(path, "example:\n  file.managed:\n    - name: /tmp/example\n")
    result = complete(client, path)
    managed = next(i for i in result["items"] if i["label"] == "managed")
    assert managed["textEdit"] == {
        "range": {
            "start": {"line": 1, "character": 7},
            "end": {"line": 1, "character": 14},
        },
        "newText": "managed",
    }
    assert "name" in managed["detail"]
    assert "3008.5" in managed["detail"]
    assert "salt/states/file.py" in managed["documentation"]
    assert "provider availability" in managed["documentation"]
    assert "managed" in managed["documentation"].lower()


def test_partial_modules_insert_dot_without_doubling_existing_suffix(
    tmp_path, clients
):
    client = clients(
        tmp_path,
        settings={
            "projects": [
                {
                    "root": tmp_path.as_uri(),
                    "profiles": {"default": {"stateRoots": ["."]}},
                }
            ]
        },
    )
    path = tmp_path / "partial.sls"
    client.open(path, "example:\n  fi\n")
    result = complete(client, path, character=4)
    item = next(i for i in result["items"] if i["label"] == "file")
    assert item["textEdit"]["newText"] == "file."
    assert item["textEdit"]["range"] == {
        "start": {"line": 1, "character": 2},
        "end": {"line": 1, "character": 4},
    }
    second = tmp_path / "existing.sls"
    client.open(second, "example:\n  fi.managed:\n")
    existing = complete(client, second, character=4)
    item = next(i for i in existing["items"] if i["label"] == "file")
    assert item["textEdit"]["newText"] == "file"


def test_live_profiles_select_one_version_per_document_without_stale_entries(
    tmp_path, clients
):
    other = tmp_path / "other"
    other.mkdir()
    projects = [
        {
            "root": tmp_path.as_uri(),
            "activeProfile": "3006",
            "profiles": {
                v: {"saltVersion": v, "stateRoots": ["."]}
                for v in ("3006", "3007", "3008")
            },
        },
        {
            "root": other.as_uri(),
            "profiles": {"default": {"stateRoots": ["."]}},
        },
    ]
    client = clients(tmp_path, settings={"projects": projects})
    path = tmp_path / "a.sls"
    text = "example:\n  chocolatey.:\n  docker_container.:\n  dsc_resource.:\n  test.:\n"
    client.open(path, text)
    second = other / "b.sls"
    client.open(second, "example:\n  dsc_resource.:\n")
    for version in ("3006", "3007", "3008", "3006"):
        projects[0]["activeProfile"] = version
        client.notify(
            "workspace/didChangeConfiguration",
            {"settings": {"salt": {"projects": projects}}},
        )
        assert client.context(path)["saltVersion"] == version
        chocolatey = complete(client, path, line=1, character=13)["items"]
        assert ("bootstrapped" in {i["label"] for i in chocolatey}) == (
            version != "3006"
        )
        docker = complete(client, path, line=2, character=19)["items"]
        assert ("running" in {i["label"] for i in docker}) == (
            version != "3008"
        )
        dsc = complete(client, path, line=3, character=15)["items"]
        assert ("managed" in {i["label"] for i in dsc}) == (version == "3008")
        test = complete(client, path, line=4, character=7)["items"]
        detail = next(
            i["detail"]
            for i in test
            if i["label"] == "configurable_test_state"
        )
        assert ("allow_test_mode_failure=False" in detail) == (
            version != "3006"
        )
        for items in (chocolatey, docker, dsc, test):
            assert all(i["data"]["version"] == version for i in items)
        assert "managed" in {
            i["label"] for i in complete(client, second, character=15)["items"]
        }
        assert client.context(second)["saltVersion"] == "3008"


def test_aliases_conditional_providers_and_conservative_contexts(
    tmp_path, clients
):
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
    states = tmp_path / "states"
    states.mkdir()
    path = states / "a.sls"
    client.open(path, "example:\n  file.:\n  cmd.:\n  x509.:\n")
    file_items = complete(client, path)["items"]
    names = {i["label"] for i in file_items}
    assert "copy" in names and "copy_" not in names
    assert not any(n.startswith("mod_") for n in names)
    watch = next(
        i
        for i in complete(client, path, line=2, character=6)["items"]
        if i["label"] == "watch"
    )
    assert "Alias of wait" in watch["documentation"]
    certs = [
        i
        for i in complete(client, path, line=3, character=7)["items"]
        if i["label"] == "certificate_managed"
    ]
    assert {i["data"]["provider"] for i in certs} == {
        "salt/states/x509.py",
        "salt/states/x509_v2.py",
    }
    assert len({i["detail"] for i in certs}) == 2
    for index, (text, line, character) in enumerate(
        [
            ("example:\n  file.managed:\n    - name: file.\n", 2, 17),
            ("include:\n  - file.\n", 1, 9),
            ("example:\n  # file.\n", 1, 9),
            (
                "example:\n  file.managed:\n    - contents: |\n        file.\n",
                3,
                13,
            ),
            ("example:\n  {{ file. }}\n", 1, 10),
            ("file.\n", 0, 5),
        ]
    ):
        negative = states / f"negative{index}.sls"
        client.open(negative, text)
        result = complete(client, negative, line, character)
        assert not result or not any(
            i.get("data", {}).get("kind") == "state" for i in result["items"]
        )
    for directory in ("pillar", "generic"):
        negative = tmp_path / directory / "a.sls"
        client.open(negative, "example:\n  file.:\n")
        assert complete(client, negative) is None
