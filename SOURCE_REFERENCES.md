# Literal source lookup

SLS includes and top-file entries use the active profile's ordered `stateRoots`
or `pillarRoots`. Dotted names resolve to `name.sls` or `name/init.sls`; both
candidates are retained if both exist in the first matching root. Leading dots
are relative to the containing SLS package (`.child`, `..sibling`). An explicit
environment (`- base: name`) must match the configured active environment; the
server never guesses roots for an unconfigured environment. Switch profiles to
analyze another environment.

Quoted literal Jinja `include`, `import`, and `from` paths use the same configured
source roots as the containing document, including non-SLS templates. Computed
expressions, concatenations, and runtime loaders are not evaluated. Currently
supported YAML references are block-list includes and block-list top entries.

Open buffers are authoritative, including empty, invalid, and not-yet-saved
sources. Each request observes current disk contents, root ordering, and profile
selection. Disk dependencies are not inserted into the editor's open-document
store. Completion omits top files, root `init.sls`, malformed dotted names,
duplicates, and the current source.

Source lookup is bounded to 2,048 directory entries / dependency edges, 128
dependency documents, 1 MiB per document, and 16 MiB per request snapshot. Include
closures are iterative and cycle-safe. Directory traversal does not follow
symlink directories; canonical target paths must remain inside explicitly
configured roots and the owning project/profile. Exhausting a bound yields
partial results, never an assertion that a runtime target is invalid. No Salt,
Jinja, or project Python is executed.
