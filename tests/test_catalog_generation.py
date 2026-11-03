"""Controlled generation CLI and its portable, provider-preserving artifacts."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile


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
