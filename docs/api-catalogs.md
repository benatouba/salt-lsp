# Offline API catalogs

State module/function mapping-key completion uses the document's active
`saltVersion`, as configured in [project profiles](project-configuration.md).
The visible default is **3008**, not a detected Salt installation. Invocation
and `.`-triggered requests both work, including unfinished state keys. Edits
replace the module or function token, preserve existing suffixes/colons, and
module insertion adds a dot when needed. State hooks (`mod_*`) remain in the
metadata but are not ordinary authoring suggestions. Pillar, generic files,
parameters, comments, includes and template expressions are not state keys.
Hover and argument completion use the same catalog: see
[below](#hover-and-argument-completion).

Catalog membership is **known API information, not proof of provider
availability** on any minion. Dependencies, OS, configuration, virtual hooks
and decorators can prevent loading or transform a callable. Conflicting
providers are separate suggestions with distinct source/signature details;
the server does not select a host provider or union version catalogs. A live
profile switch selects the new version on the next request without reopening.
3007 compatibility does **not** mean ongoing upstream support. Consult the
[upstream lifecycle policy](https://docs.saltproject.io/salt/install-guide/en/latest/topics/salt-version-support-lifecycle.html)
independently of this static metadata.

## Hover and argument completion

`textDocument/hover` on a state call shows the signature, the exact release, the
provider path and line, and the catalog documentation as plain text, because the
docstrings are reStructuredText. A function with several conditional providers
shows every candidate: the server doesn't pick one. Argument defaults are source
expressions, and a function the docstring leaves undocumented says so.

Completion at a list entry directly under a call, such as `- sou` or `- `, offers
that function's argument names. The edit replaces only the name, adds `: ` when
no colon follows and adds the missing space after a bare `-`. Required arguments
sort first. Names already used in the same call aren't offered, except the entry
being edited. If a function has several providers, each name lists the providers
that declare it.

Both recognize these literal forms, under a state ID or under `extend`:

```yaml
one:
  file.managed:        # also with an indentless list, or before the colon is typed
    - name: /tmp/a
two:
  file:                # hover works on the key and on `managed`
    - managed
    - name: /tmp/b
three: file.managed    # hover only
```

Templates are lexed, never rendered. A generated state ID (`{{ id }}:`) still has
a literal call beneath it, and control lines between arguments are ignored. Text
inside a multi-line tag, a comment or a captured block (`{% load_yaml %}`,
`{% set x %}`) is not state source, so it gets no assistance. An unclosed tag ends
with its line, so it can't disable later states.

Unknown stays quiet. The response is empty, not an error, for a function missing
from the active catalog, a module key without exactly one known function, a
templated call name, nested data, block scalars, flow-style lists, and pillar,
generic and top files.

Limits:

- Only arguments named in the source signature are offered. Requisites, global
  state arguments such as `order`, and names accepted through `**kwargs` aren't
  listed, which is why `module.run` offers nothing.
- Argument documentation is extracted from docstrings by recognizing their common
  layouts: about 84-86% of named arguments per catalog. The rest are offered with
  no documentation rather than a guess.
- Arguments in both branches of an `{% if %}` count as already specified.
- Hover covers the call, not individual argument names.
- Function-name completion (`file.`) doesn't use this template handling. It offers
  nothing under a state ID that begins with `{{` and can fire inside a multi-line
  tag.

## Pins and provenance

`tools/catalog-sources.json` locks official PyPI source distributions, their
SHA-256 digests, exact releases and corresponding peeled upstream commits:

| Profile | Release | Public state sources | Source-known provider entries |
| --- | --- | ---: | ---: |
| 3006 | 3006.30 | 356 | 1240 |
| 3007 | 3007.17 | 354 | 1218 |
| 3008 | 3008.5 | 129 | 447 |

Counts include aliases, cloned declarations and conditional provider candidates,
not runtime availability. Each inventory additionally includes `__init__.py`.
The much smaller 3008 catalog reflects upstream core removals: APIs are not
backfilled from older releases. Salt documentation is Apache-2.0 licensed;
upstream notices are retained beside the packaged catalogs, and the Apache
license is distributed with the package.

Each `salt_lsp/catalogs/states-VERSION.json.gz` is deterministic JSON (schema 1)
containing input provenance, generator SHA-256, entries and a coverage inventory.
Entries preserve kind (`state`), exposed module/function names, Python name,
provider/definition path and line, alias target, full source docstrings,
decorators, authoring eligibility, return annotation and argument metadata.
Defaults are **source expressions**, never evaluated objects; `has_default`
distinguishes a required argument from literal `None`. Positional-only,
positional-or-keyword, keyword-only, variadic arguments and annotations survive.
`kind + name + provider + line` identifies a candidate, not just `name`.
This shared metadata shape leaves room for later execution/Jinja APIs; this
ticket does not implement execution completion or custom-module indexing.

## Controlled regeneration (development only)

1. Read/review the lock. Download the three exact `url` artifacts in it as plain
   HTTP files into an archive directory, retaining each `artifact` filename.
   Do not install/build Salt or use package-manager download hooks.
2. From the checkout, run with CPython 3.12 (no third-party packages needed):

   ```sh
   python3.12 tools/generate_catalogs.py --archives /path/to/archives \
     --output salt_lsp/catalogs
   ```

3. Run generation fixtures and stdio tests, repeat generation into another
   directory and compare byte-for-byte, then build sdist → wheel and run
   `tests/installed_smoke.py` from a runtime-only environment without Salt.

Generation itself has no network access. It verifies archive checksums, rejects
unsafe paths, duplicate source paths, links in source inputs, oversized files
and inventories, reads archive members without extraction, and fails on parse
errors. It parses Python ASTs only, without imports, loaders, subprocesses or
`__virtual__` execution. It never reads a user's project. The source distribution
includes the generator/lock; wheels need only the resulting catalogs.
No host paths, wall-clock timestamps or host-dependent provider filtering are
emitted. Gzip headers have a fixed time and no original filename.

## Coverage and limits

The generator inventories **every state source** in the pinned archive, on
all host platforms. It handles module-scope definitions (including conditional
suites, excluding nested functions/classes), literal `__load__`/`__all__`
restrictions (including empty lists and literal append/extend), function aliases,
literal virtual names/return strings and virtual aliases, local function
assignment aliases, `salt.utils.functools.alias_function`, and namespace clones
of directly imported pinned-source definitions. Only verified archive paths
are used for clones; imports alone are not public exports. Hook code is retained
as source metadata rather than evaluated. Supporting source hashes identify
the other archive Python inputs used for definition lookup.

This is source-known coverage, **not exhaustive runtime/dynamic enumeration**.
The inventory records source hashes, module docs, virtual hook source, metadata
declarations, excluded private/restricted functions, imports, unresolved public
assignment expressions, decorated signatures and dynamic export possibilities.
An unresolved expression is not evidence that it is callable or absent. Known
recognizers are deliberately narrow; new patterns require review and fixtures,
not evaluation. Decorator transformations and arbitrary export mutations,
factories, indirect import/clone chains or computed virtual names are not
resolved. Ordinary logging/constants appear in the unresolved-value audit too;
that audit is conservative, not a claim they are exports. It is not a formal
proof that arbitrary Python cannot manufacture further runtime functions.

In particular, 3006/3007 `testinframod.py` has an initially empty export list and
generates states from an optional dependency. Its dynamic export sites are
reported; no dependency-owned names are invented. Ansible execution collections
are likewise outside a fixed core state catalog. The fixed `ansible` state APIs
are included. Provider alternatives such as legacy/v2 `x509` are preserved,
not collapsed according to filesystem order or the generator host.
