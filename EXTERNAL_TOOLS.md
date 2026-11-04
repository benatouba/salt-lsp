# Optional external tools

Static assistance needs no Salt or optional tools. The server never installs
tools, imports project Python or renders Jinja. Executables and custom lint rules
are trusted code, **not sandboxed code**.

## Select, inspect, approve

Example `initializationOptions.salt` (also accepted as configuration `settings.salt`):

```json
{"tools": [
  {"projectRoot": "/work/project", "profile": "default", "name": "salt-lint",
   "executable": "/opt/salt-lint-lsp/bin/salt-lint", "config": "lint.yaml",
   "ruleDirs": ["lint-rules"]},
  {"projectRoot": "/work/project", "name": "yamlfmt",
   "executable": "/opt/yamlfmt", "config": "format.yaml"}
]}
```

Profile defaults to `default`. Omit config to use an owned temporary `{}` config,
never ancestor/HOME discovery. An omitted executable is discovered only on safe,
absolute non-project PATH entries. Workspace executables need explicit selection
and approval. Config/rule paths are project-root relative. External config/rules
also require `allowSharedConfig: true`. A salt-lint config's `rulesdir` must exactly
match separately selected `ruleDirs`; without a config these are passed to the CLI.

1. Request `salt/toolStatus` with URI and `tool: "salt-lint"` or `"yamlfmt"`.
2. Display returned executable/config/rules/context provenance to the user.
3. **Only after a user gesture**, request `salt/authorizeTool` with URI, tool,
   returned token, and `action: "allow"`. Never auto-approve from configuration.
4. `salt/toolProbe` explicitly checks the approved tool. `salt/lintStatus` reports
   pending/clean/findings or sanitized failure for an open state document.
5. Revoke with `salt/authorizeTool`, `action: "revoke"`, `token: ""`.

Approvals are session-only and bound to content/selection/context identities.
Configuration, profile, executable, config or rule changes invalidate them. Watch
events refresh idle lint; edits also recheck selection. Configs are bounded YAML
mappings (64 KiB). Rule trees have content/entry budgets and reject internal
symlinks/special files. Cwd is always the project root, including external config.
HOME/XDG use owned scratch space; Python injection variables are removed. Processes
have timeout/output/concurrency limits and group cleanup. Status never forwards
raw stdout/stderr or tracebacks.

## Patched salt-lint: manual installation

**Stock salt-lint cannot safely lint unsaved buffers**: its stdin tempfile loses
the original filename. The approved native adapter patch is
[`tools/salt-lint-0.9.2-lsp.patch`](tools/salt-lint-0.9.2-lsp.patch), against upstream
v0.9.2 commit `ad1a4df81204321b3067c7587aec6405b690dddd`. It adds an explicit-config
`--stdin-filename PATH` mode and a capability handshake before config/custom-rule
loading. There is no Python bridge, file overwrite or I/O interception.

Install manually in a separate environment; the server never does this:

```sh
git clone https://github.com/warpnet/salt-lint.git /opt/src/salt-lint-lsp
git -C /opt/src/salt-lint-lsp checkout --detach ad1a4df81204321b3067c7587aec6405b690dddd
git -C /opt/src/salt-lint-lsp apply --check --unidiff-zero /absolute/salt-lsp/tools/salt-lint-0.9.2-lsp.patch
git -C /opt/src/salt-lint-lsp apply --unidiff-zero /absolute/salt-lsp/tools/salt-lint-0.9.2-lsp.patch
uv venv --python 3.12 /opt/salt-lint-lsp
uv pip install --python /opt/salt-lint-lsp/bin/python --no-deps /opt/src/salt-lint-lsp PyYAML==6.0.3 pathspec==1.1.1
/opt/salt-lint-lsp/bin/salt-lint --salt-lsp-capabilities
```

The exact supported contract is protocol 1, salt-lint 0.9.2, CPython 3.12,
PyYAML 6.0.3 and pathspec 1.1.1. Other combinations are unverified and refused.
This does not narrow the server's own supported Python matrix.

Native custom callbacks receive unsaved text and the original absolute filename.
Original filename classification, exclusions and config behavior are retained.
A rule reopening that filename sees **saved disk**, not the unsaved buffer; it
must use supplied text for current edits. Relative rule resources use project cwd.

Approved open state documents lint after one second without edits. Edits, close,
reopen, profile/trust/config changes cancel obsolete work. Old lint findings clear
immediately on edits. Valid findings have source `salt-lint`, native IDs and
warning severity over the original source line. Clean results clear only lint;
built-in diagnostics remain. Malformed JSON, invalid filenames/lines, config or
process failure produce status, not clean results or invented source findings.
Input limit is 1 MiB, with shared process/output limits. At most 128 documents
are scheduled at once; later edits can schedule documents as slots free.
Workspace-wide lint is not supported.

## Formatting and real compatibility tests

Google yamlfmt **0.21.0** supports only the narrow plain-YAML scope documented in
[FORMATTING.md](FORMATTING.md). No Jinja formatter has been verified.

```sh
SALT_LSP_TEST_SALT_LINT=/opt/salt-lint-lsp/bin/salt-lint SALT_LSP_TEST_YAMLFMT=/opt/yamlfmt uv run --locked pytest -q tests/test_linting.py tests/test_formatting.py tests/test_tools.py
```

The subprocess LSP tests exercise real custom-rule text/path/resources, empty
buffers and original-path exclusions, plus controlled trust/debounce/cancellation
and current-buffer formatting. Ordinary static requests never run project code.
