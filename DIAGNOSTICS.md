# Diagnostic ownership and confidence

The initial built-in provider publishes `source: salt-lsp` for unambiguous state
documents in the active project/profile. It does not compile or render Salt.

* `duplicate-id` (error): repeated string state IDs inside one unconditional,
  template-free YAML mapping. Never computed across files or Jinja branches.
* `include-not-list` (error): a literal, nonempty `include` value that is not a
  sequence in template-free YAML. An unfinished empty include is left alone.
* `unresolved-sls` (information): a dotted literal include has neither a file nor
  directory-form source in the configured active roots. A runtime loader may
  still provide it. Missing/unreadable roots, existing unreadable files, escaping
  paths, inactive environments, relative/dynamic includes and template-loader
  paths are not diagnosed. At most 128 references are checked per document.

Ranges refer to the original editor text and negotiated position encoding.
Documents over 1 MiB are not analyzed. YAML errors are not republished: syntax,
style and rule diagnostics belong to optional external tools. Their findings
use their own source and are merged, not overwritten, by the server.

Unknown functions are never errors: packaged catalogs are not exhaustive and
custom sources only describe possible declarations, not runtime availability.
Generated values, ambiguous state/pillar roles, and uncertain requisites are not
treated as broken compilation. No workspace-wide duplicate-ID check is made.

Analysis is synchronous against current source and context. Changes to any open
dependency refresh open state documents; configuration changes refresh or clear
them. Clients should send `workspace/didChangeWatchedFiles` for disk dependency
changes. Close publishes an empty set. No background built-in result can race a
newer document version. Optional asynchronous tools must additionally validate
the document lifetime/version and immutable project context before publishing.
