# salt-lsp

[![Continous Integration](https://github.com/dcermak/salt-lsp/actions/workflows/ci.yml/badge.svg)](https://github.com/dcermak/salt-lsp/actions/workflows/ci.yml)
[![VSCode Extension Build](https://github.com/dcermak/salt-lsp/actions/workflows/vscode_extension.yml/badge.svg)](https://github.com/dcermak/salt-lsp/actions/workflows/vscode_extension.yml)
[![Code Coverage](https://img.shields.io/codecov/c/github/dcermak/salt-lsp)](https://app.codecov.io/gh/dcermak/salt-lsp)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://black.readthedocs.io/en/stable/)
[![PyPI](https://img.shields.io/pypi/v/salt-lsp)](https://pypi.org/project/salt-lsp/)
[![Visual Studio Marketplace](https://img.shields.io/visual-studio-marketplace/v/dancermak.salt-lsp)](https://marketplace.visualstudio.com/items?itemName=dancermak.salt-lsp)

Salt Language Server Protocol Server

## History & Current State

This is a Hackweek project of [Cédric Bosdonnat](https://github.com/cbosdo) and
[Dan Čermák](https://github.com/dcermak) that reached an early prototype
state. However, we never really managed to handle Jinja2 templating in `sls`
files (see [`salt-lsp#31`](https://github.com/dcermak/salt-lsp/pull/31)) which
is a requirement for this project to be even remotely useful.

Unfortunately neither of the two original authors currently have the time to
keep this project alive. If you wish to help out, please open an issue or send a
pull request fixing the existing bugs/shortcomings.

As of now, here be 🐉.


## Server installation and launch

The modernized runtime supports **CPython 3.11–3.14** on Linux. CI tests every
minor version with **pygls 2.0.1**, **lsprotocol 2025.0.0**, and **PyYAML 6.0.3**.
Other operating systems are not yet acceptance-tested. Salt, salt-lint, an
editor extension, and generated metadata are **not** installation requirements.
The server runtime is independent of the Salt version a project targets.

Build this checkout with [uv](https://docs.astral.sh/uv/) and install the wheel
into a dedicated environment (the fork is not yet a new PyPI release):

```sh
uv sync --locked --python 3.12
uv build --no-sources
uv export --locked --no-dev --no-emit-project -o /tmp/salt-lsp-runtime.txt
uv venv --python 3.12 /path/to/salt-lsp-env
uv pip install --python /path/to/salt-lsp-env/bin/python \
  --constraint /tmp/salt-lsp-runtime.txt dist/*.whl
```

`uv.lock` records the complete tested dependency graph, including transitive
and development dependencies; runtime versions are also pinned in the package.
Builds use the pinned Hatchling backend, not Poetry. `uv build` creates an sdist
and then builds its wheel, so no developer-only metadata is needed. To update
dependencies deliberately, update the pins, regenerate `uv.lock`, and run the
matrix before widening support.

Configure any standard LSP client to launch either of these **stdio** commands:

```sh
/path/to/salt-lsp-env/bin/salt_lsp_server
/path/to/salt-lsp-env/bin/python -m salt_lsp
```

Use language ID `sls` for Salt documents. Do not add `--tcp` for stdio clients.
Stdout contains only Content-Length-framed JSON-RPC; logging goes to stderr,
not into the user's workspace. `--log-level warning` reduces logging. TCP mode
(`--tcp --host 127.0.0.1 --port 2087`) remains available for development only.

Startup deliberately uses an **empty API metadata fallback**. Document symbols
and existing source-based features work; state-function metadata completion is
empty until the versioned catalog work lands. This is not a claim that a Salt
API version was detected. Old ignored `salt_lsp/data/states.pickle` files are
never loaded, even if present; do not run the legacy metadata generator to
start the server. Missing optional metadata cannot prevent initialization.

Document synchronization supports full replacements and sequential incremental
edits. Source locations use UTF-16 by default, or negotiated UTF-8/UTF-32,
including non-BMP characters and LF, CRLF, or CR line endings. Closing a document
discards its buffer analysis; reopening analyzes the new contents. Unsupported
document URI schemes (such as `untitled:`) receive empty assistance, not errors.
File URIs decode percent-encoded paths and recognize localhost, drive, and UNC
forms; Windows filesystem behavior is not yet acceptance-tested.

### Runtime checks

```sh
uv run --locked pytest
# Use the isolated artifact environment above, not the editable development one:
/path/to/salt-lsp-env/bin/python tests/installed_smoke.py
/path/to/salt-lsp-env/bin/python tests/installed_smoke.py \
  /path/to/salt-lsp-env/bin/salt_lsp_server
```

The dependency-free smoke probe asserts Salt is absent, launches from an empty
temporary directory with `PYTHONPATH` removed, exchanges real initialize,
didOpen, document-symbol, completion, shutdown and exit messages, checks a
known symbol and deterministic empty completion, and requires a clean exit
without workspace writes. Each response and process exit has a timeout.

## Project configuration

[Configure projects and active profiles](docs/project-configuration.md) through
standard LSP initialization options or live settings. Project boundaries are
separate from state/pillar roots and custom-module directories. Named profiles
select one Salt version/environment/root set; the visible default is Salt 3008.
Configure state roots explicitly to enable state-specific assistance—unconfigured
or ambiguous files are not guessed to be states. Settings never execute Salt,
templates, or workspace Python. The portable JSON schema ships in
[`salt_lsp/project.schema.json`](salt_lsp/project.schema.json).

# Development

`devenv.nix` declares the development environment; Salt is deliberately absent.
`devenv shell` provides the locked dependencies, `yamlfmt`, Neovim and a
pre-commit hook (managed by `prek`) that runs the test suite on every commit.

- `check-static` runs Black, mypy and pylint.
- `check-matrix` runs the tests and installed-artifact smoke sessions on every
  supported Python, in fresh environments bound to the current checkout.

# Clients

## VSCode

1. Run `yarn install`
2. Start VSCode and open this directory as the server
3. Open the Debug menu (Ctrl + Shift + D)
4. Select "Launch Client" drop down
5. Press F5


## Emacs

You should install [salt-mode](https://github.com/glynnforrest/salt-mode) so
that `sls` files are registered to the salt major mode. The language server must
be installed on your system so that the clients can launch it.

### Using lsp-mode

1. Install & configure [lsp-mode](https://github.com/emacs-lsp/lsp-mode/)
2. Load the file `clients/emacs/salt-lsp.el`
3. Open a sls file and launch `lsp` via `M-x lsp`


### Using eglot

1. Install & configure [eglot](https://github.com/joaotavora/eglot)
2. Evaluate the following snippet:
```elisp
(add-to-list 'eglot-server-programs '(salt-mode . ("python3" "-m" "salt_lsp")))
```
3. Launch eglot via `M-x eglot`
