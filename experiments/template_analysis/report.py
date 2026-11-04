"""Reproduce representative evidence through the guarded executable seam."""

from collections import Counter
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).parent
evidence = {
    "python": ".".join(map(str, sys.version_info[:2])),
    "dependencies": {
        name: importlib.metadata.version(name)
        for name in ("PyYAML", "Jinja2", "MarkupSafe")
    },
    "fixtures": {},
}
for fixture in sorted((HERE / "fixtures").glob("*.sls")):
    response = subprocess.run(
        [sys.executable, "-I", str(HERE / "guard.py")],
        input=fixture.read_bytes(),
        capture_output=True,
        check=True,
    )
    result = json.loads(response.stdout)
    evidence["fixtures"][fixture.name] = {
        "comparisons": result["comparisons"],
        "declarations": [
            {
                key: item[key]
                for key in ("kind", "value", "certainty", "start", "end")
            }
            for item in result["declarations"]
        ],
        "references": [item["value"] for item in result["references"]],
        "unknowns": dict(
            sorted(
                Counter(item["kind"] for item in result["unknowns"]).items()
            )
        ),
    }
serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
if sys.argv[1:] == ["--check"]:
    if serialized != (HERE / "evidence.json").read_text():
        sys.exit("Evidence differs; run report.py and inspect the changes.")
    print("Evidence matches all guarded fixture results.")
else:
    print(serialized, end="")
