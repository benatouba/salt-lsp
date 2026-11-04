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
entries, unreadable routes, and every path or symlink hop through any editor
folder or configured project root (`..` is applied the way the kernel applies it,
after symlinks). Unsafe candidates do not mask later installed candidates. Normal
outside-to-outside installation symlinks (including Nix profiles) are supported.
A tool installed under a project root, such as `~/.local/bin` when the repository
is the home directory, is not discovered: select it explicitly instead.
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
   canonical executable, explicit/PATH and workspace provenance, config, rules,
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
`cleanup-failure` means a process escaped the group and kept its pipes open, or
could not be stopped: the output may be incomplete and must not be used.

## Stable Python provider API (#17 / #18)

Use **`server.tools: salt_lsp.tools.ToolService`**, not subprocess APIs or the
lower-level process primitive. Public methods are `inspect(uri, name)`,
`authorize(uri, name, token, action)`, `invalidate()`, `await close()`,
`on_invalidate(callback)`, and:

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
`name`, `explicit`, `workspace_executable`, and an opaque `identity`.
`invocation(selection, config)`
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
It rechecks after waiting for capacity, and checks again before returning
output. While a job runs, the document stamp and grant are checked every 50 ms;
the selected files are reread every 500 ms, because that hashes the whole
selection on the event loop. Profile/context, config/rule edits, executable
changes, document edits/close, and revocation suppress old results. Changes observed as invalid also forget previous grants; restoring old
contents does not resurrect an observed-invalid selection's approval.

`ProcessResult` has `status`, `message`, `stdout: bytes`, `stderr: bytes`, and
`returncode`. `message` is always set by `run()`, is actionable, and never contains
child output, so show it instead of writing your own text for a status. Only use
raw output after checking the expected status/return code. A nonzero exit is
`tool-failure` with the output retained. A lint
adapter may explicitly accept a nonzero findings exit code, but must never treat
timeout, overflow, cancellation, stale, or malformed output as clean diagnostics.
Parsing, diagnostics ownership, lint debounce, formatter eligibility/edits, and
publication-time staleness checks remain the consumer's responsibility. Recheck
the stamp/context before publishing after any additional await or expensive
parsing. Register `unsubscribe = server.tools.on_invalidate(clear_tool_results)`
to clear only tool-owned results on explicit revoke/settings changes or an
observed selection invalidation. The callback is synchronous server code,
receives no source, and must leave built-in diagnostics intact. There are at
most 16 subscriptions; call `unsubscribe()` when disposing a provider.
Revocation does not disable static analysis.

Cancellation follows normal asyncio rules. Cancelling the task awaiting `run()`,
including through `asyncio.timeout`, stops the process group, waits for cleanup
and then raises `CancelledError`, so a superseding debounce can simply cancel and
await its previous task. When the *service* stops a job (revocation, settings, or
an observed change), `run()` instead returns a `cancelled` or `stale` result.

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
  the leader already exited: TERM grace 100 ms, then KILL grace 100 ms, only while
  group members remain. Output is then drained to EOF for up to 300 ms, so nothing
  already written is lost and a result is complete. If EOF does not arrive, an
  escaped descendant holds the pipes: they are closed within a further 100 ms and
  the status is `cleanup-failure`. A clean exit costs no grace time. The
  concurrency slot stays held through cleanup.
- Cleanup absorbs cancellation, including the single cancellation of every task
  at event-loop teardown, and is awaited, never detached. The LSP `shutdown`
  request, the `exit` notification, stdin EOF and SIGTERM all stop running groups
  before the stdio server process ends. SIGKILL of the server cannot; an orphaned
  tool then sees closed pipes but may keep running. stdout/stdin never inherit
  LSP framing.
- Launch environment removes every `PYTHON*` variable and `VIRTUAL_ENV`, enables
  safe Python path/no-user-site/no-bytecode, filters project/relative PATH entries,
  and gives HOME/XDG
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

`tests/test_tools.py` and `tests/test_tools_boundary.py` use actual stdio LSP
sessions, controlled executable processes, and the public provider/process seams.
No private runner state is asserted. Run the real installed compatibility/config probe as well:

```sh
SALT_LSP_TEST_YAMLFMT=/absolute/user/installed/yamlfmt pytest tests/test_tools.py
```

That optional test requires 0.21.0 and never installs it. Without the variable it
is explicitly skipped; controlled executable tests always run.
