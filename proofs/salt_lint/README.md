# Unsaved salt-lint compatibility proof (#3)

## Result: blocked, not a production adapter

**The unmodified salt-lint 0.9.2 executable cannot faithfully lint an unsaved
buffer with the original file's path semantics.** This proof takes ticket #3's
explicit blocker-reporting route. It does not implement save-only linting,
publish diagnostics, or claim that output-path remapping solves input fidelity.
Ticket #17 (debounced lint integration) must not treat this proof as a usable
adapter. Resolve the compatibility boundary first; ticket #16's trusted tool
runner/configuration work also needs the findings below.

Declared compatibility target: **salt-lint 0.9.2**, exercised on CPython 3.12.15
with PyYAML 6.0.3 and pathspec 0.12.1. No other release is claimed supported.
This is a supported *probe target*, not a supported faithful unsaved adapter.
The published wheel `salt_lint-0.9.2-py3-none-any.whl` has SHA-256
`d9dbf137a2ef917e0a6110d50bc79b132d5a35464cea943c891a30dd4412c6b2`.
The environment does not contain Salt or require server development assets.

## Reproduce

From the repository root, use an existing Python environment containing the
versions in `proofs/salt_lint/requirements.txt`. For example, explicitly create
an isolated proof environment (these are manual installation commands, not
behavior of the probe):

```sh
nix shell nixpkgs#python312 --command uv venv --python python3 /tmp/opencode/ticket-3-venv
uv pip install --python /tmp/opencode/ticket-3-venv/bin/python -r proofs/salt_lint/requirements.txt
SALT_LINT_EXECUTABLE=/tmp/opencode/ticket-3-venv/bin/salt-lint \
  /tmp/opencode/ticket-3-venv/bin/python -B -m unittest discover -s proofs/salt_lint -v
```

On non-Nix systems, create the environment with your Python 3.12 instead.
Tests use POSIX pseudo-terminals to run saved-file reference invocations without
salt-lint's implicit stdin input. All fixtures live under `/tmp/opencode`.
No server packaging files or runtime modules are changed. The standalone suite
deliberately avoids `tests/conftest.py` and its old server dependencies.

For a single investigation with **trusted** configuration/custom rules:

```sh
/tmp/opencode/ticket-3-venv/bin/python -B proofs/salt_lint/probe.py \
  --executable /tmp/opencode/ticket-3-venv/bin/salt-lint \
  --filename /absolute/project/states/example.sls \
  --config /absolute/project/selected-salt-lint.yaml < /path/to/buffer-text
```

The JSON report always has `faithful: false`; CLI exit status is always **3**
(incompatibility), including when `outcome` is `clean`. The named source file
need not exist and is never opened for writing. No tool is installed or
downloaded automatically.

## Observed evidence

The test suite compares real saved-file CLI results with different unsaved
stdin content, rather than inferring behavior from the documentation.

| Case | Saved-file/reference behavior | Unsaved stdin behavior |
| --- | --- | --- |
| Clean buffer | No output, exit 0 | No output, exit 0 |
| Unsaved trailing whitespace on line 2 | Disk is clean | Rule 201 reports unsaved text at line 2, exit 2 |
| `bad.name.sls` | Rule 214 reports invalid SLS filename | Missing finding: random temporary basename lacks the period |
| `template.jinja` | Rule 205 reports a non-SLS extension | Missing finding: temporary `.sls` suffix changes the rule result |
| Relative/absolute `exclude_paths` for original file | File excluded | File checked: exclusion sees temporary path |
| Rule-specific original-path `ignore` | Rule suppressed | Rule runs: ignore sees temporary path |
| `.sls`, `.SLS` | Classification `sls` | `sls` |
| `.jinja`, `.jinja2`, `.j2` | Classification `jinja` | Incorrectly `sls` |
| `.yaml`, `.txt`, no extension | Classification `None` | Incorrectly `sls`; these are not upstream-recognized Salt/Jinja classifications |
| Selected config in another directory | Relative `rulesdir` and rule-relative file reads use explicit cwd | Same cwd and rule loading preserved, but custom rule sees temporary source path |
| Unrelated ancestor / HOME `.salt-lint` | Not selected | Not consumed when cwd is the selected config's directory |
| Invalid explicitly selected YAML config | Configuration error | Recognized separately; leaked upstream stdin file removed by enclosing cleanup |
| Invalid **cwd** `.salt-lint`, even with another `-c` | Tool import fails | Tool fails before argument parsing; `--version` fails too |
| Config `json: false` despite `--json` | Config overrides CLI output mode | Non-JSON findings rejected as malformed, not clean |
| Trusted custom rule prints to stdout | Output contaminated | Rejected as malformed, not clean |
| Trusted custom rule raises on import | Execution failure | Execution failure, no findings accepted, owned temp files cleaned |

The observing custom rule receives the unsaved text and reads a relative marker
from the explicitly chosen configuration directory. Its finding message embeds
the path, classification, and cwd it actually received. Remapping the finding's
`filename` leaves the wrong path inside that message: the rule already made its
decision using the wrong input. This proves that remapping output is insufficient.
Even a mirror that preserves the basename/suffix cannot preserve arbitrary
custom rules comparing absolute paths or reading neighboring project resources.

Controlled executable fixtures additionally cover malformed JSON/schema,
foreign diagnostic paths, missing executables, unsupported tool versions, and
timeout cleanup. They supplement, not replace, real-release compatibility tests.
Each probe test snapshots workspace file contents, inodes, modification times,
and directory entries, and verifies an empty owned scratch directory afterward.

## Probe contract and cleanup

`probe(executable, filename, text, config, *, temp_root, timeout=10)` is the
external-process investigation seam. Paths are explicit; `config` must exist.
There is no discovery or fallback to an ancestor/global configuration. Execution
uses `config.parent` as cwd. **Upstream paths are cwd-relative, not inherently
config-relative**: choosing this cwd deliberately defines config-relative
semantics. A future project-root-relative contract must instead normalize those
paths explicitly; do not accidentally change this behavior during integration.

The probe verifies the version, then invokes `--json --nocolor -c CONFIG` with
buffer text on stdin and no positional file. It records raw and illustrative
remapped findings, stdout, stderr, exit code, and an outcome:
`clean`, `findings`, `configuration_error`, `malformed_output`,
`execution_error`, or `unsupported_version`. Missing selected config is caught
before invocation. It only remaps validated paths directly inside the owned
temporary directory, never arbitrary paths emitted by custom rules. Config
failures recognizable from upstream's `invalid config:` output are distinct
from crashes; not all invalid config types are reported cleanly by upstream.

The invocation owns a `TemporaryDirectory` outside the workspace and redirects
`TMPDIR`, `TMP`, and `TEMP` there. `PYTHONDONTWRITEBYTECODE=1` prevents Python
custom-rule imports from leaving workspace `__pycache__` artifacts. Cleanup
encloses version checking, stdin execution, parsing, nonzero exits, and timeout
handling, including salt-lint's early-error temporary-file leaks. No source
overwrite, temporary workspace file, symlink, or mirror is used. Process crashes
or power loss cannot promise cleanup; arbitrary trusted custom rules can perform
their own writes or spawn children. This is not a sandbox or the future bounded
process-tree runner. Trust must be granted **before** calling the probe.

## Concrete upstream boundary and required follow-up

Evidence is pinned to the distributed 0.9.2 source:

- [`saltlint/cli.py`](https://github.com/warpnet/salt-lint/blob/v0.9.2/saltlint/cli.py):
  `run()` unconditionally turns non-TTY stdin into a random `NamedTemporaryFile`
  with `.sls` suffix. `init_argument_parser()` has no stdin-filename option.
  Configuration failures return before the file's unlink step.
- [`saltlint/linter/runner.py`](https://github.com/warpnet/salt-lint/blob/v0.9.2/saltlint/linter/runner.py):
  classification and exclusions consume that same physical path.
- [`saltlint/linter/collection.py`](https://github.com/warpnet/salt-lint/blob/v0.9.2/saltlint/linter/collection.py):
  `run()` opens that physical path, applies per-rule ignores to it, and passes it
  to custom rules. `RulesCollection.__init__(config=Configuration())` evaluates
  a default `.salt-lint` configuration at module import, before explicit `-c`.
- [`saltlint/config.py`](https://github.com/warpnet/salt-lint/blob/v0.9.2/saltlint/config.py):
  relative rule directories/exclusions use process cwd, and config can override
  the requested JSON output mode.

A compatible executable needs a supported text-plus-logical-filename boundary
(for example, `--stdin-filename ORIGINAL`) that applies the logical path to
**classification, filename rules, exclusions, per-rule ignores, custom-rule
inputs, and diagnostics**, while supplying the buffer text without overwriting
the physical source. It must also avoid import-time implicit configuration,
support explicit configuration without hidden defaults, guarantee JSON mode,
and clean stdin storage on every exit path. Tests must compare that new boundary
to saved-file semantics, including custom rules, before enabling diagnostics.

A deliberately versioned external library bridge could alternatively be
designed and verified, but 0.9.2's `RulesCollection.run()` has no public text
parameter. Copying its implementation, monkey-patching file I/O, using a
namespace/filesystem overlay, or assuming custom rules never inspect paths is
not evidence of a compatible installed salt-lint CLI. Those approaches require
an explicit new adapter/deployment contract and their own proof. No upstream
issue was filed and no save-only substitution was made by this ticket.
