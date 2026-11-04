"""Run the headless Neovim acceptance against a Salt-free installed wheel.

Usage: /installed/env/bin/python tests/installed_neovim.py
Optional real tools: SALT_LSP_TEST_{SALT_LINT,YAMLFMT} absolute executables.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    assert (
        importlib.util.find_spec("salt") is None
    ), "Use a Salt-free environment"
    package = importlib.util.find_spec("salt_lsp")
    assert package and Path(package.origin).resolve().is_relative_to(
        Path(sys.prefix).resolve()
    ), "Use an installed wheel, not the checkout"
    assert shutil.which("nvim"), "Install Neovim 0.11+ to run acceptance"
    with tempfile.TemporaryDirectory(prefix="salt-lsp-neovim-") as directory:
        root = Path(directory)
        (root / "_states").mkdir()
        (root / "_modules").mkdir()
        (root / "_states" / "acme.py").write_text(
            "raise RuntimeError('PROJECT SOURCE MUST NOT EXECUTE')\ndef managed(name, old=None):\n    '''Local state documentation.'''\n    pass\n"
        )
        (root / "_modules" / "acme.py").write_text(
            "raise RuntimeError('PROJECT SOURCE MUST NOT EXECUTE')\ndef lookup(name):\n    '''Local execution documentation.'''\n    pass\n"
        )
        (root / "known.sls").write_text("target:\n  test.nop: []\n")
        (root / "state.sls").write_text(
            "example:\n  file.managed:\n    - sou\n"
        )
        tools = [
            {
                "projectRoot": str(root),
                "name": name,
                "executable": os.environ.get(
                    variable, str(root / ("missing-" + name))
                ),
            }
            for name, variable in (
                ("salt-lint", "SALT_LSP_TEST_SALT_LINT"),
                ("yamlfmt", "SALT_LSP_TEST_YAMLFMT"),
            )
        ]
        settings = {
            "projects": [
                {
                    "root": str(root),
                    "profiles": {
                        "default": {
                            "stateRoots": ["."],
                            "saltVersion": "3008",
                        },
                        "older": {"stateRoots": ["."], "saltVersion": "3006"},
                    },
                }
            ],
            "tools": tools,
        }
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update(
            SALT_LSP_ACCEPTANCE_ROOT=str(root),
            SALT_LSP_ACCEPTANCE_COMMAND=str(
                Path(sys.prefix) / "bin" / "salt_lsp_server"
            ),
            SALT_LSP_ACCEPTANCE_SETTINGS=json.dumps(settings),
        )
        subprocess.run(
            [
                "nvim",
                "--headless",
                "-u",
                "NONE",
                "-l",
                str(
                    Path(__file__).with_name("neovim_acceptance.lua").resolve()
                ),
            ],
            cwd=root,
            env=env,
            check=True,
            timeout=90,
        )
        assert (
            root / "state.sls"
        ).read_text() == "example:\n  file.managed:\n    - sou\n"
        assert "fresh" not in (root / "_states" / "acme.py").read_text()
    print("Installed Salt-free Neovim acceptance passed")


if __name__ == "__main__":
    main()
