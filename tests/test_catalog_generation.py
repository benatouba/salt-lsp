"""Controlled generation CLI and its portable, provider-preserving artifacts."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


GENERATOR = Path(__file__).resolve().parents[1] / "tools/generate_catalogs.py"


def generate(tmp_path, sources):
    archive = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path, text in sources.items():
            data = text.encode()
            info = tarfile.TarInfo("fixture/" + path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            [
                {
                    "profile": "fixture",
                    "release": "fixture",
                    "artifact": archive.name,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            ]
        )
    )
    output = tmp_path / "output"
    command = [
        sys.executable,
        str(GENERATOR),
        "--lock",
        str(lock),
        "--archives",
        str(tmp_path),
        "--output",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True)
    result = output / "states-fixture.json.gz"
    first = result.read_bytes()
    subprocess.run(command, check=True, capture_output=True)
    assert result.read_bytes() == first
    return json.loads(gzip.decompress(first))


def test_static_clone_retains_provider_and_defining_source(tmp_path):
    catalog = generate(
        tmp_path,
        {
            "salt/states/example.py": """
raise RuntimeError("source must never execute")
from salt.modules.upstream import external
from salt.utils.functools import namespaced_function
__virtualname__ = "sample"
def __virtual__():
    return True
if unknown_platform():
    cloned = namespaced_function(external, globals())
""",
            "salt/modules/upstream.py": '''
raise RuntimeError("dependency source must never execute")
def external(name, /, value=UNRESOLVED, *args, required, optional=None, **kwargs):
    """Upstream source documentation."""
''',
        },
    )
    assert [e["name"] for e in catalog["entries"]] == ["sample.cloned"]
    entry = catalog["entries"][0]
    assert entry["provider"] == "salt/states/example.py"
    assert entry["definition"]["path"] == "salt/modules/upstream.py"
    assert entry["documentation"] == "Upstream source documentation."
    assert entry["arguments"] == [
        {
            "name": "name",
            "kind": "positional_only",
            "annotation": None,
            "has_default": False,
            "default": None,
        },
        {
            "name": "value",
            "kind": "positional_or_keyword",
            "annotation": None,
            "has_default": True,
            "default": "UNRESOLVED",
        },
        {
            "name": "args",
            "kind": "var_positional",
            "annotation": None,
            "has_default": False,
            "default": None,
        },
        {
            "name": "required",
            "kind": "keyword_only",
            "annotation": None,
            "has_default": False,
            "default": None,
        },
        {
            "name": "optional",
            "kind": "keyword_only",
            "annotation": None,
            "has_default": True,
            "default": "None",
        },
        {
            "name": "kwargs",
            "kind": "var_keyword",
            "annotation": None,
            "has_default": False,
            "default": None,
        },
    ]


def test_literal_export_extensions_and_dynamic_unknowns_are_not_silently_lost(
    tmp_path,
):
    catalog = generate(
        tmp_path,
        {
            "salt/states/dynamic.py": '''
__all__ = []
__all__.append("fixed")
def fixed(name):
    """Fixed source-known API."""
def not_exported(name):
    pass
for external_name in discover_dependency():
    __all__.append(external_name)
    globals()[external_name] = make_wrapper(external_name)
'''
        },
    )
    assert [e["name"] for e in catalog["entries"]] == ["dynamic.fixed"]
    report = catalog["coverage"][0]
    assert report["dynamic_exports"]
    assert any(
        "external_name" in d["expression"] for d in report["dynamic_exports"]
    )
    assert {e["name"] for e in report["excluded_functions"]} == {
        "not_exported"
    }


def test_virtual_aliases_export_precedence_and_provider_signatures(tmp_path):
    catalog = generate(
        tmp_path,
        {
            "salt/states/one.py": '''
"""First provider's module documentation."""
__virtualname__ = "shared"
__virtual_aliases__ = ("alternate",)
__func_alias__ = {"copy_": "copy"}
def __virtual__():
    return True
def copy_(name, *, force=False):
    """Copy safely."""
    def nested_local():
        pass
watch = salt.utils.functools.alias_function(copy_, "watch", doc="Custom alias docs.")
''',
            "salt/states/two.py": '''
def __virtual__():
    return "shared"
@dependency_gate("optional")
def copy(name, other=None):
    """Second provider."""
''',
            "salt/states/restricted.py": """
__load__ = []
__all__ = ["must_not_export"]
def must_not_export(name):
    pass
""",
        },
    )
    names = [e["name"] for e in catalog["entries"]]
    assert sorted(names) == [
        "alternate.copy",
        "alternate.watch",
        "shared.copy",
        "shared.copy",
        "shared.watch",
    ]
    copies = [e for e in catalog["entries"] if e["name"] == "shared.copy"]
    assert {e["provider"] for e in copies} == {
        "salt/states/one.py",
        "salt/states/two.py",
    }
    assert copies[0]["arguments"][1]["name"] == "force"
    assert copies[1]["arguments"][1]["name"] == "other"
    assert copies[1]["decorators"] == ["dependency_gate('optional')"]
    assert (
        next(e for e in catalog["entries"] if e["name"] == "shared.watch")[
            "documentation"
        ]
        == "Custom alias docs."
    )


@pytest.mark.parametrize(
    "unsafe", ["hash", "../escape.py", "/absolute.py", "link"]
)
def test_generation_rejects_unverified_or_unsafe_source_archives(
    tmp_path, unsafe
):
    generate(tmp_path, {"salt/states/example.py": "def valid(name): pass\n"})
    archive = tmp_path / "fixture.tar.gz"
    if unsafe == "hash":
        archive.write_bytes(b"not the verified archive")
    else:
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo(
                "fixture/salt/states/linked.py" if unsafe == "link" else unsafe
            )
            if unsafe == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
            tar.addfile(member)
        lock = json.loads((tmp_path / "lock.json").read_text())
        lock[0]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        (tmp_path / "lock.json").write_text(json.dumps(lock))
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--lock",
            str(tmp_path / "lock.json"),
            "--archives",
            str(tmp_path),
            "--output",
            str(tmp_path / "rejected"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not list((tmp_path / "rejected").glob("*.gz"))
    assert (
        "checksum mismatch"
        if unsafe == "hash"
        else "unsafe archive path" if unsafe != "link" else "regular file"
    ) in result.stderr
