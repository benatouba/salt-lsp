"""Investigate salt-lint 0.9.2 stdin; NOT a faithful production adapter.

Run only with explicit user trust: selected configuration can import Python rules.
"""

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SUPPORTED_VERSION = "salt-lint 0.9.2"


@dataclass
class ProbeResult:
    outcome: str
    faithful: bool = False
    findings: list = field(default_factory=list)
    raw_findings: list = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    limitation: str = "stdin has no original-path or classification input"


def probe(executable, filename, text, config, *, temp_root, timeout=10):
    """Observe the unmodified CLI with explicit config and isolated temp files.

    Relative rulesdirs/exclusions use config.parent as cwd (upstream semantics
    are cwd-relative, NOT automatically config-relative). Remapped findings are
    illustrative only; the faithful flag deliberately never becomes true.
    """
    filename = Path(filename).absolute()
    config = Path(config).absolute()
    if not config.is_file():
        return ProbeResult("configuration_error", stderr="selected config missing")
    executable = str(Path(executable).absolute())
    with tempfile.TemporaryDirectory(prefix="salt-lint-proof-", dir=temp_root) as temp:
        env = dict(os.environ, TMPDIR=temp, TMP=temp, TEMP=temp,
                   PYTHONDONTWRITEBYTECODE="1")
        try:
            version = subprocess.run(
                [executable, "--version"], cwd=config.parent, env=env,
                input="", text=True, capture_output=True, timeout=timeout,
                check=False,
            )
            if version.returncode:
                return ProbeResult("execution_error", stdout=version.stdout,
                                   stderr=version.stderr,
                                   returncode=version.returncode)
            if version.stdout.strip() != SUPPORTED_VERSION:
                return ProbeResult("unsupported_version", stdout=version.stdout,
                                   stderr=version.stderr,
                                   returncode=version.returncode)
            process = subprocess.run(
                [executable, "--json", "--nocolor", "-c", str(config)],
                cwd=config.parent, env=env, input=text, text=True,
                capture_output=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult("execution_error", stderr=str(exc))
        result = ProbeResult("malformed_output", stdout=process.stdout,
                             stderr=process.stderr, returncode=process.returncode)
        if process.returncode == 2 and process.stdout.startswith("invalid config:"):
            result.outcome = "configuration_error"
            return result
        if process.returncode not in (0, 2) or process.stderr:
            result.outcome = "execution_error"
            return result
        if process.returncode == 0 and not process.stdout.strip():
            result.outcome = "clean"
            return result
        try:
            items = json.loads(process.stdout)
            if not isinstance(items, list):
                return result
            for item in items:
                if not isinstance(item, dict):
                    return result
                if not all(isinstance(item.get(k), str) for k in
                           ("id", "message", "filename", "line", "severity")):
                    return result
                if type(item.get("linenumber")) is not int or item["linenumber"] < 1:
                    return result
                # Only remap paths owned by this invocation, never arbitrary
                # paths emitted by a custom rule.
                path = Path(item["filename"])
                if not path.is_absolute() or path.parent != Path(temp):
                    return result
            if (process.returncode == 0) != (not items):
                return result
        except (ValueError, TypeError):
            return result
        result.raw_findings = items
        result.findings = [dict(item, filename=str(filename)) for item in items]
        result.outcome = "findings" if items else "clean"
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--temp-root", default="/tmp/opencode")
    args = parser.parse_args()
    result = probe(args.executable, args.filename, sys.stdin.read(), args.config,
                   temp_root=args.temp_root)
    print(json.dumps(asdict(result), indent=2))
    # No successful faithful adapter should be inferred from a clean probe.
    return 3


if __name__ == "__main__":
    sys.exit(main())
