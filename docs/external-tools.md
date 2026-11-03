# Optional external-tool boundary

Static analysis never requires Salt, salt-lint, or a formatter. The server never
downloads tools, imports workspace Python, or treats a project/profile/module
directory as permission to execute. Linux is currently the only supported
external-execution platform; other platforms retain static features and return
`unsupported-platform` rather than claiming unimplemented tree supervision.

## Portable selection (not permission)

Add `tools` to the `salt` settings snapshot described in
[project-configuration.md](project-configuration.md):

```json
{
  "salt": {
    "tools": [{
      "projectRoot": "/home/me/formulas",
      "profile": "default",
      "name": "salt-lint",
      "executable": "/home/me/.local/bin/salt-lint",
      "config": ".salt-lint",
      "ruleDirs": ["lint_rules"],
      "allowSharedConfig": false
    }]
  }
}
```

`projectRoot` and `executable` are absolute filesystem paths, **not URIs**.
`profile` defaults to `default`; selection matches the current project root and
active profile exactly. There must be exactly one matching entry. Supported
names are `salt-lint` and `yamlfmt`. Config/rule paths are literal and relative
to the project root. There is no shell, tilde, variable, or glob expansion.
Unknown settings, including `trusted`, are errors and disable tool selections
until corrected. Tools settings fail closed, unlike recoverable static-project
settings. Every received Salt configuration snapshot revokes previous grants.

Omitting `executable` searches absolute PATH entries, skipping empty/relative
entries and every path or symlink hop through any editor folder or configured
project root. Unsafe candidates do not mask later installed candidates. Normal
outside-to-outside installation symlinks (including Nix profiles) are supported.
Explicit selection never falls back and may deliberately name a workspace tool;
the user must still approve it. Invocation preserves virtualenv symlink spelling
and revalidates executable identity before launch.

No Saltfile/master/minion/Python configuration or native ancestor/global config
search is used. Select the config explicitly or adapters receive a server-owned
empty YAML mapping in a temporary directory. A config must be a regular YAML
mapping of at most 64 KiB. Rules are scanned without imports: at most 1024
entries and 4 MiB **across all rule directories**. Internal rule-tree symlinks
and special files are refused. Canonical config/rule paths outside the project
require `allowSharedConfig: true` **and a new explicit approval**. This setting
does not grant execution or authorize other external paths.

For salt-lint, config `rulesdir` must be a list whose resolved paths exactly
match the separately selected `ruleDirs`. Relative paths use **project-root
cwd**, including when the selected config is elsewhere. Do not silently change
this to config-parent cwd. yamlfmt has no custom-rule adapter.

## Editor trust and status protocol

The editor, not repository settings, owns the user gesture:

1. Request `salt/toolStatus` with `{uri, tool}`. This does **not** execute any
   process, including a version probe. A resolvable selection returns
   `denied-trust`, an opaque `token`, and `selection`: executable spelling,
   canonical executable, explicit/PATH provenance, config, rule directories,
   project root, and profile. Display these subjects to the user.
2. Only after explicit user approval, request `salt/authorizeTool` with
   `{uri, tool, token, action: "allow"}`. Approval covers **all** displayed
   subjects, including executable and custom-rule code, not just the binary.
   Tokens are session-local, bounded, and pinned to executable metadata,
   config/rule contents and the immutable project context. A stale token cannot
   approve a changed subject. No boolean or initialization setting grants trust.
3. Status is now `ready`, which means **permitted**, not compatible or clean.
   `salt/toolProbe` with `{uri, tool}` performs the permitted compatibility probe.
4. Request `salt/authorizeTool` with `action: "revoke"` (same parameter shape)
   to revoke all session grants before cancelling running/queued work. Shutdown
   does the same and awaits cleanup. Revoke is not rollback of past side effects.

`salt/toolProbe` supports real yamlfmt **0.21.0** version negotiation using
`-conf ABSOLUTE_CONFIG -no_global_conf -version`, with project-root cwd.
Older/newer/unrecognizable output is `unsupported-capability`, not clean.
This does not establish that arbitrary Salt/Jinja source is formattable.

salt-lint intentionally returns `unsupported-capability` **without executing a
stock version probe**, even after permission. The approved deployment requires
an explicitly selected, patched, capability-negotiated salt-lint; the stock
0.9.2 version string cannot identify that patch and its import-time config
discovery is unsafe. Ticket #17 must register its verified invocation through
the shared runner, negotiate its distinct capability and supported dependencies,
and maintain the reproducible patch/setup instructions. There is no stock
fallback, automatic installation, or workspace-import bridge here.

Statuses distinguish `missing-tool`, `denied-trust`, `invalid-config`, `ready`,
`clean`, `unsupported-capability`, `unsupported-platform`, `spawn-failure`,
`execution-failure`, `tool-failure`, `timeout`, `input-limit`, `output-limit`,
`busy`, `cancelled`, `stale`, and `cleanup-failure`. Messages are sanitized;
child stdout/stderr, source text, config contents, and exception output are never
logged or returned in LSP status. Static features remain available on failure.

## Stable Python provider API (#17 / #18)

Use **`server.tools: salt_lsp.tools.ToolService`**, not subprocess APIs or the
lower-level process primitive. Public methods are `inspect(uri, name)`,
`authorize(uri, name, token, action)`, `invalidate()`, `await close()`, and:

```python
result = await server.tools.run(
    uri,
    "yamlfmt",
    lambda selection, config: ["-conf", str(config), "-no_global_conf", "-in"],
    version=captured_document_stamp,
    current_version=lambda: workspace.document_stamp(uri),
    data=unsaved_source.encode("utf-8"),
    timeout=5.0,
    output_limit=262144,
)
```

`Selection` is immutable and exposes `context`, `executable`, `config`, `rules`,
`name`, `explicit`, and an opaque `identity`. `invocation(selection, config)`
is **server adapter code**, never a client/workspace-supplied callback or argv.
It must use the supplied absolute config to disable native discovery, preserve
the approved tool-specific rule semantics, and enforce tool/dependency/capability
compatibility before consuming results. For salt-lint, retain original filename
plus unsaved text; native rule callbacks may deliberately read saved source from
disk. Do not overwrite source, intercept I/O, or implement an overlay.

`version`/`current_version` represent an immutable document lifetime/version
stamp, not merely a reusable LSP integer (see workspace `document_stamp`). Use
`None` only for work without document contents, such as version probes. They
are mandatory, preventing consumers from accidentally omitting stale-result
checks. The service also captures the full `ProjectContext` and trust epoch.
It rechecks after waiting for capacity, monitors validity every 50 ms while
running, and checks again before returning output. Profile/context, config/rule
edits, executable changes, document edits/close, and revocation suppress old
results. Changes observed as invalid also forget previous grants; restoring old
contents does not resurrect an observed-invalid selection's approval.

`ProcessResult` has `status`, `stdout: bytes`, `stderr: bytes`, and `returncode`.
Only use raw output after checking the expected status/return code. A lint
adapter may explicitly accept a nonzero findings exit code, but must never treat
timeout, overflow, cancellation, stale, or malformed output as clean diagnostics.
Parsing, diagnostics ownership, lint debounce, formatter eligibility/edits, and
publication-time staleness checks remain the consumer's responsibility. Recheck
the stamp/context before publishing after any additional await or expensive
parsing. Clear only tool-owned results when trust is revoked; leave built-in
diagnostics intact. Revocation does not disable static analysis.

## Resource and lifecycle guarantees

- One shared runner per server: two active processes and eight waiting jobs;
  excess work returns `busy`. Consumers should replace superseded pending work,
  not accumulate tasks outside this boundary.
- Input is capped at 4 MiB. Default output retention is 256 KiB **per stream**,
  caller-adjustable up to 1 MiB each. Capture counts bytes, drains both streams
  independently, and promptly stops overflow. No unbounded `communicate()` or
  temporary output file. Input is chunked with backpressure and explicit EOF.
- Default execution deadline is 5 s, clamped to 10 ms–30 s. Queue wait is bounded
  by queue length and preceding jobs, not included in execution time. Kernel
  spawn/filesystem operations are not a hard real-time deadline guarantee.
- Every invocation gets a new Linux process group. Normal exit, timeout,
  overflow, cancellation, revoke and shutdown clean the **whole group**, even if
  the leader already exited: TERM grace 100 ms, KILL grace 100 ms, pipe/transport
  join up to 300 ms. Public transport ownership closes pipes if an escaped
  descendant holds descriptors. The concurrency slot stays held through cleanup.
- Shielded, strongly owned spawn/cleanup tasks survive repeated cancellation;
  cleanup is awaited, never detached. stdout/stdin never inherit LSP framing.
- Launch environment removes Python path/startup overrides, enables safe Python
  path/no-user-site, filters project/relative PATH entries, and gives HOME/XDG
  an owned temporary directory. Other environment variables remain for normal
  user installations and Nix wrappers. This is not a hermetic environment.

This is **not a sandbox**: approved executables/config/custom rules are trusted
code and can perform arbitrary side effects, ignore environment controls, or
escape a process group via setsid. Canonical checks and metadata/content pins
do not defeat same-user races, hard links, mount manipulation, mutable installed
dependencies, or hostile trusted programs. Hard containment needs OS sandboxing.
External edits are observed on inspection/launch and the bounded polling cycle;
there is no filesystem watch or promise to detect edits changed back between
observations. Never present approval or revocation as preventing all side effects.

## Acceptance tests

`tests/test_tools.py` uses actual stdio LSP sessions, controlled executable
processes, and the public provider/process seams. No private runner state is
asserted. Run the real installed compatibility/config probe as well:

```sh
SALT_LSP_TEST_YAMLFMT=/absolute/user/installed/yamlfmt pytest tests/test_tools.py
```

That optional test requires 0.21.0 and never installs it. Without the variable it
is explicitly skipped; controlled executable tests always run.
