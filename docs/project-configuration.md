# Projects and active profiles

Optional executable/config selection is documented separately in
[external-tools.md](external-tools.md). `salt.tools` settings select subjects;
they never grant execution trust.

Salt LSP uses the same JSON settings in every editor. Send the object below as
`initialize.initializationOptions`, or as `workspace/didChangeConfiguration`
`params.settings`. No editor extension or Salt installation is required.

```json
{
  "salt": {
    "projects": [
      {
        "workspaceUri": "file:///home/me/formulas",
        "root": ".",
        "activeProfile": "production",
        "profiles": {
          "production": {
            "saltVersion": "3006",
            "environment": "prod",
            "stateRoots": ["salt/prod", "salt/common"],
            "pillarRoots": ["pillar/prod"],
            "moduleDirs": ["extensions/states", "extensions/modules"],
            "fileRoles": {
              "examples/service.sls": "state",
              "salt/common/data.sls": "pillar",
              "templates/dispatch.sls": "generic"
            }
          },
          "next": {
            "saltVersion": "3008",
            "environment": "base",
            "stateRoots": ["salt/next"],
            "pillarRoots": ["pillar/next"]
          }
        }
      }
    ]
  }
}
```

The machine-readable schema for the **contents of `salt`** is
[`salt_lsp/project.schema.json`](../salt_lsp/project.schema.json). It is also
included in the installed Python package. Runtime validation additionally checks
local URI/path validity, unique project scopes, and active-profile membership.
Unknown keys are errors, helping catch typos instead of silently ignoring them.

## Selection and paths

- **Project root** is an authoring boundary, not a state or pillar tree. An
  explicit `root` wins over Git and editor discovery. It may be a local absolute
  path, a local `file:///` URI, or a path relative to `workspaceUri`.
- Without `root`, discover the nearest enclosing `.git` **directory or file**
  from the document's directory upwards. Git/worktree metadata is not executed
  or followed to infer content roots. No `git` executable is required.
- Outside Git, use the deepest enclosing editor workspace folder; `rootUri` is
  the fallback if the client provides no folders. Without an enclosing folder,
  use the document's directory. Untitled/non-local documents have no context.
- `workspaceUri` selects the editor scope of an entry and must be a local file
  URI. Omit it when an absolute `root` identifies the scope, or in a single-folder
  session where the editor folder is unambiguous. In multi-folder sessions,
  relative roots require `workspaceUri`. The deepest matching configured scope
  wins; entries with duplicate scopes are invalid. Use explicit nested entries
  to give nested projects independent profiles.
- `stateRoots`, `pillarRoots`, `moduleDirs`, and `fileRoles` paths are resolved
  relative to the **selected project root**, never relative to each other.
  Absolute paths and local file URIs are accepted. Missing directories are
  allowed so configuration works before a checkout is complete.
- Explicit roots may name external content/source directories. A uniquely owned
  external source retains its explicit project's context. A shared external
  source with no unique owner is generic, not whichever profile was listed
  first. To associate external documents with a project, configure its `root`
  explicitly rather than relying on a workspace-wide discovery template.
- Paths are literal: no shell, glob, tilde, environment-variable expansion,
  imports, renderer evaluation, or Salt calls. Paths are canonicalized (including
  symlinks); URIs returned to the editor are absolute file URIs. Only local
  files are supported; operating-system support remains the README's matrix.

Neither a `top.sls` file nor the shape of a repository implicitly establishes a
state root. The server does not load Saltfile, master/minion configuration,
Python configuration, or a project configuration file automatically. Editors
may load an explicit JSON file themselves and send its data through standard
LSP settings; the server only receives data, not executable editor code.

## Defaults and file roles

An omitted `profiles` is equivalent to `{"default": {}}`; `activeProfile`
defaults to `"default"` and must name exactly one existing profile. There is no
union of inactive versions, roots, or environments, nor inheritance between
profiles. Configure versions as strings: `"3006"`, `"3007"`, or `"3008"`.
Version selection does not assert upstream support or minion availability.

Each profile defaults to Salt **3008**, environment **base**, and **empty** root,
module-directory, and role lists. Salt 3008 is an explicit assumption, not a
detected runtime: it is logged at startup and exposed with `versionSource:
"default"` in the context response. Module directories are source locations,
not directories that the server imports. Custom-module indexing and bundled
catalogs are separate capabilities, not implemented by this settings layer.

Only `.sls` files unambiguously inside state roots receive state assistance.
Files only inside pillar roots are pillar data. A `top.sls` inside one kind of
root is `state-top` or `pillar-top`. Files in both kinds of roots, outside known
roots, or with other extensions are `generic` unless overridden. Rich pillar
semantics are not provided. Generic/pillar files are not interpreted as state
declarations: state symbols, function completion, and requisite navigation are
withheld rather than guessed. Generic YAML/Jinja processing can be added without
changing this role contract; the current server does not promise a general YAML
language server.

`fileRoles` is an object mapping **exact paths**, not patterns, to `state`,
`pillar`, `state-top`, `pillar-top`, or `generic`. Overrides take priority over
root membership. Configured roots, not the project boundary or `top.sls`
discovery, supply the existing include/requisite lookup and include completion.
Comprehensive include ambiguity, traversal/dependency behavior, and richer
template assistance remain separate tickets.

## Switching and errors

To switch, change `activeProfile` and send the **whole `salt` snapshot** in
`workspace/didChangeConfiguration`. This replaces settings, not a recursive
patch. An empty `salt` object removes explicit configuration; omitted entries
are removed. Unrelated editor settings outside `salt` are not inspected; a
notification without a `salt` section leaves Salt settings unchanged.

Changed project contexts receive a new opaque `revision`. Already-open buffers
are reclassified immediately and affected parsed/include/symbol results are
refreshed from current buffers. Unchanged projects retain their context and
revision. Workspace-folder additions/removals update discovery and affected
results. Clients request fresh results through normal LSP methods; profile
switching requires no server restart or document reopen.

Invalid settings emit actionable standard `window/showMessage` errors naming
the settings path. A malformed outer snapshot retains the last valid snapshot.
An invalid project entry retains its last valid entry by scope (or previous
index if its scope cannot be decoded); on first use it falls back to discovery
with generic roles. Other valid entries still apply. Fix the settings and resend
to recover without restarting. No invalid configuration disables the server.

## Public selection contract (catalog and tool providers)

Any client can send `salt/projectContext` with `{"uri": "file:///.../a.sls"}`.
No open buffer is required. The response is `null` for unsupported/non-local
URIs; otherwise it is an object with these fields:

```json
{
  "projectRoot": "file:///home/me/formulas",
  "activeProfile": "production",
  "saltVersion": "3006",
  "versionSource": "configured",
  "environment": "prod",
  "stateRoots": ["file:///home/me/formulas/salt/prod"],
  "pillarRoots": ["file:///home/me/formulas/pillar/prod"],
  "moduleDirs": ["file:///home/me/formulas/extensions/states"],
  "fileRole": "state",
  "revision": 1
}
```

Python providers use `server.workspace.project_context(uri)` returning immutable
`salt_lsp.project.ProjectContext | None`. The dataclass uses snake_case and
`Path`/tuple values; `to_lsp()` serializes the contract above. The underlying
`ProjectModel.context(uri, folder_uris)` is a public non-executing selector.
The workspace owns invalidation; providers should not duplicate root discovery
or read private workspace fields.

For asynchronous work, capture the context **and document version**, then compare
both before publishing. Cache per `(project_root, active_profile, revision)`;
never cache solely by Salt version, environment, or workspace folder. Revisions
are opaque, process-local change tokens, not persistent IDs or trust grants.
Compare the full context where file roles affect a result. Tool trust and tool
configuration boundaries must be explicit additional policy: a selected project
or configured module directory is **not authorization to execute anything**.
