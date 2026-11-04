# Formatting

`textDocument/formatting` (including editor format-on-save) uses a separately
installed **Google yamlfmt 0.21.0**. Select it in `salt.tools` and explicitly
approve the displayed executable/config selection using `salt/authorizeTool`.
Settings cannot grant trust. See [EXTERNAL_TOOLS.md](EXTERNAL_TOOLS.md) for the selection protocol.

Only open state-role documents in the active profile are supported. Input must
be standard, marker-free YAML (at most 1 MiB). Any standard Jinja delimiter,
including incomplete expressions, comments, raw blocks and quoted strings,
causes refusal. Tags, directives, malformed scanning and custom `#!` renderers
are refused. The only recognized renderer declarations are `#!yaml` and
`#!jinja|yaml`; these do not permit Jinja content. Custom template delimiters or
line-statement syntaxes are **not supported**: do not enable this formatter for
projects using them. There is no verified template-aware formatter or setting
that claims Jinja safety.

The adapter supplies explicit selected (or isolated empty) configuration and
`-no_global_conf -in`; it never supplies a source filename or write flag. Cwd
is the project root. Native path/exclusion filters do not apply to stdin: an
explicit editor request is the eligibility decision. Output is UTF-8 and bounded
to 2 MiB; timeout is five seconds. The editor receives one full-document edit,
or no edits for unchanged input, stale/closed documents, changed profiles or
trust revocation. Refusals and failures produce an actionable editor warning,
never raw subprocess output. Nothing directly rewrites the file.

Yamlfmt's own newline/default formatting policy applies (normally LF). LSP
indent options do not override its selected configuration. No generic Python
formatting, format orchestration or other editor integrations are installed.

Real compatibility tests are opt-in:

```sh
SALT_LSP_TEST_YAMLFMT=/absolute/path/to/yamlfmt uv run --locked pytest -q tests/test_formatting.py tests/test_tools.py
```
