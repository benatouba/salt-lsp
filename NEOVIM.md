# Salt-free Neovim acceptance

Use Neovim 0.11+ and an **installed wheel**, following [README.md](README.md).
No Salt, editor plugin, Python language server, or optional executable is needed
for static assistance. The settings below are ordinary LSP initialization/settings
data; other clients can use the same project model.

## Minimal client configuration

Replace both absolute paths. A project root is not automatically a state root.
Do not enable optional tools until their executable/configuration has been reviewed.

```lua
vim.filetype.add({ extension = { sls = 'sls' } })
local root = '/absolute/project'
local salt = {
  projects = {{
    root = root,
    activeProfile = 'default',
    profiles = {
      default = { saltVersion = '3008', environment = 'base', stateRoots = {'states'} },
      older = { saltVersion = '3006', environment = 'base', stateRoots = {'states'} },
    },
  }},
}
vim.lsp.config('salt', {
  cmd = { '/absolute/salt-lsp-env/bin/salt_lsp_server' },
  filetypes = { 'sls' },
  root_dir = root,
  init_options = { salt = salt },
})
vim.lsp.enable('salt')
```

Use Neovim's built-in omnifunc/completion, `K` hover, `gd` definition,
`vim.lsp.buf.document_symbol()` and `vim.diagnostic.open_float()`. These need no
Salt-specific plugin. Switch profiles by updating the same settings object:

```lua
salt.projects[1].activeProfile = 'older'
for _, client in ipairs(vim.lsp.get_clients({ name = 'salt' })) do
  client:notify('workspace/didChangeConfiguration', { settings = { salt = salt } })
end
```

## Repeatable automated acceptance

From this checkout, using the isolated installed interpreter (not `uv run`):

```sh
/absolute/salt-lsp-env/bin/python tests/installed_smoke.py
/absolute/salt-lsp-env/bin/python tests/installed_neovim.py
```

The second command starts `nvim --headless -u NONE` with an isolated temporary
project and standard LSP client. It checks installed/Salt-free provenance,
profile changes, built-in hover/arguments, Jinja execution-function hover,
outlines, literal includes and requisites, diagnostics, unsaved custom Python
metadata/definition positions and provenance, empty generic Python assistance,
missing optional tools, template-format refusal and clean shutdown. Source
fixtures raise if imported. Disk contents must remain unchanged.

To additionally test **real**, separately installed optional executables:

```sh
SALT_LSP_TEST_SALT_LINT=/absolute/patched-env/bin/salt-lint \
SALT_LSP_TEST_YAMLFMT=/absolute/yamlfmt \
  /absolute/salt-lsp-env/bin/python tests/installed_neovim.py
```

Only the documented patched salt-lint and Google yamlfmt 0.21.0 are supported;
see [EXTERNAL_TOOLS.md](EXTERNAL_TOOLS.md) and [FORMATTING.md](FORMATTING.md).
This test grants trust **only inside its disposable fixture project**. It checks
native lint diagnostics after the trailing debounce, clearing obsolete findings,
and formatting edits without buffer/disk writes. Without these variables the
same test checks missing-tool degradation. `devenv shell -- check-matrix` runs
the installed Neovim session on CPython 3.11–3.14, as well as both stdio entry
points and the source tests. Set the variables for real-tool matrix coverage.
The stdio tests additionally force in-flight obsolete lint/formatter races,
revocation, malformed output and profile/context invalidation; they do not rely
on timing a fast native formatter in an interactive editor.

## Interactive acceptance checklist

1. Open `states/example.sls`. Complete `file.man`, hover `file.managed`, and
   complete `sou` under its parameter list. Switch profiles and verify the
   release/provenance changes (3008.5 versus 3006.30).
2. Add `{{ salt['test.ping']() }}` and inspect execution-function hover/completion.
   Add a Jinja conditional around a literal state: outlines should retain it,
   without rendering the template. A malformed independent stanza must not hide
   later literal states. Generated values are not proven runtime declarations.
3. Create `states/known.sls` containing `target: {test.nop: []}`. Complete
   `include: [ ... ]` using the supported block-list spelling
   `include:\n  - known`; `gd` on `known` navigates there. A requisite
   `- test: target` under `require` navigates to the declaration. Edit/delete the
   dependency and repeat; candidates must follow the current source, not a
   previously loaded compilation. See [SOURCE_REFERENCES.md](SOURCE_REFERENCES.md)
   for supported literal syntax and conservative candidate semantics.
4. Create `states/_states/acme.py` with `def managed(name, old=None): ...`.
   Use `acme.managed` in an SLS and inspect its project-source provenance and
   Python definition. Open the Python buffer and **explicitly attach the existing
   Salt client** for source synchronization:
   ```lua
   -- Run in that Python buffer; does not start or configure Python tooling.
   local client = assert(vim.lsp.get_clients({ name = 'salt' })[1])
   vim.lsp.buf_attach_client(0, client.id)
   ```
   Rename the function/arguments without saving; return to the SLS and verify
   completion, hover and `gd` follow the unsaved declaration. Closing the Python
   buffer restores disk metadata. Repeat with `states/_modules/acme.py` and a
   literal Jinja Salt call. [CUSTOM_SOURCES.md](CUSTOM_SOURCES.md) explains static
   exports and multiple providers.
5. Independently enable your chosen Python LSP/formatter through its normal
   configuration. Verify Python completion/hover/formatting still come from
   that provider. Salt does not start, proxy, or reconfigure it; its Python
   responses are empty. When explicitly synchronizing Python as above, filter
   formatting to your chosen Python client:
   ```lua
   vim.lsp.buf.format({ filter = function(client) return client.name ~= 'salt' end })
   ```
   This user-selected third-party coexistence step is manual; the automated
   check verifies Salt's non-ownership without installing a Python server.
6. In plain YAML, repeat an ID and inspect the local `salt-lsp` diagnostic;
   remove it and verify clearing. A missing literal include is informational,
   not proof a runtime loader cannot supply it. Unknown functions and mutually
   exclusive templated IDs are not hard errors. See [DIAGNOSTICS.md](DIAGNOSTICS.md).
7. Select optional tools explicitly and use the trust requests documented in
   [EXTERNAL_TOOLS.md](EXTERNAL_TOOLS.md). Inspect `salt/toolStatus`, review the
   returned selection/token, and only then send `salt/authorizeTool` with
   `action='allow'` from an explicit user gesture. Never auto-approve on attach.
   With the patched native linter, add trailing whitespace without saving;
   wait one second without edits and inspect a `salt-lint` finding. Type again
   before one second: obsolete findings must clear and only the latest snapshot
   can publish. `salt/lintStatus` distinguishes failure/unsupported tools from
   clean results. Revoke trust and verify findings clear and execution stops.
8. Request `vim.lsp.buf.format({filter=function(c) return c.name=='salt' end})`
   in a plain YAML state using approved yamlfmt. Changes arrive as ordinary LSP
   edits; saving remains the editor's responsibility. Enable format-on-save only
   by calling that same API from your own `BufWritePre` handler. Add any Jinja
   marker, including `{{ grains.id }}`: formatting must return no edits and an
   actionable refusal, never mask/restore or rewrite the file directly.
9. Remove optional tool selections or point to a missing binary. Static
   completion/navigation must continue; no implicit installation or Salt call
   occurs. Review any custom rules before trusting: external execution is bounded,
   **not sandboxed**. Keep custom-renderer/delimiter projects out of the verified
   plain-YAML formatting scope.

Interactive UI/key mappings and coexistence with an actual user-selected Python
provider are manual checks, not claimed automated results. The automated runner
prints success only after all enabled assertions and shutdown complete.
