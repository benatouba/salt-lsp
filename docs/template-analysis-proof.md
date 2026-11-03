# Non-executing YAML/Jinja proof (#2)

Parent: [spec #1](https://github.com/benatouba/salt-lsp/issues/1).
This is an executable experiment, **not a server parser replacement**. It changes
neither server dependencies nor packaging. Its public boundary is UTF-8 source on
stdin and JSON on stdout. Fixtures are explicitly state-role inputs, not arbitrary
pillar data. No template or Python file is followed from a reference.

## Reproduce

Tested with CPython 3.12.15 and the three exact versions in
`experiments/template_analysis/requirements.txt`; Salt is not installed in this
environment. From the repository root, with `uv` and Nix available:

```sh
nix shell nixpkgs#python312 --command uv venv --python python3.12 /tmp/opencode/salt-template-proof
uv pip install --python /tmp/opencode/salt-template-proof/bin/python -r experiments/template_analysis/requirements.txt
/tmp/opencode/salt-template-proof/bin/python -m unittest discover -s experiments/template_analysis -v
/tmp/opencode/salt-template-proof/bin/python -I experiments/template_analysis/proof.py < experiments/template_analysis/fixtures/mixed.sls
/tmp/opencode/salt-template-proof/bin/python -I experiments/template_analysis/report.py --check
```

Outside Nix, use `python3.12 -m venv /tmp/opencode/salt-template-proof` for the
first command (create `/tmp/opencode` if needed). No project installation or old
test dependencies are required. Always use `-I` for the executable experiment:
Python isolated mode excludes workspace paths from dependency import discovery.
The test runner also invokes its child experiments this way.

`report.py` runs every `.sls` fixture through `guard.py`, then summarizes the
JSON. To inspect/rebaseline evidence deliberately:

```sh
/tmp/opencode/salt-template-proof/bin/python -I experiments/template_analysis/report.py > /tmp/opencode/template-analysis-evidence.json
diff -u experiments/template_analysis/evidence.json /tmp/opencode/template-analysis-evidence.json
```

## Evidence and recommendation

The experiment compares **raw YAML composition**, **Jinja syntax parsing**, and
**a position-preserving Jinja projection with YAML islands** on the same inputs.
It does not assume that the existing scanner is equivalent to YAML composition,
nor claim an exhaustive comparison of all parser libraries.

| Fixture | Raw YAML | Jinja syntax | Island result (state + function headers) |
| --- | --- | --- | --- |
| `plain.sls` | accepts | accepts | 2 literal; nested data is not a function |
| `mixed.sls` | scanner error | accepts | 5 headers: 3 conditional, 2 literal; 4 literal references; generated ID unknown |
| `incomplete.sls` | parser error | syntax error | 8 headers, including 2 partial; later `after` and `service.running` survive |
| `nested.sls` | scanner error | accepts | 4 headers; nested branches conditional; bad alias isolated; Python YAML tag not executed |
| `sentinel.sls` | scanner error | accepts | `safe` and `test.nop` survive; 2 literal template references are recorded, never loaded |

Exact offsets, certainty, dependency versions, comparison errors, and unknown
categories are committed in `experiments/template_analysis/evidence.json`.
Nine subprocess-boundary tests additionally cover quoted delimiters, balanced
expression containers, raw blocks, Unicode, CRLF, dynamic references, reserved
keys, unsupported renderers, and edit prefixes at template/quote/bracket
boundaries. Development used incremental red→green slices (plain ranges, mixed
templates, damaged islands, quoted/raw delimiters, and reserved-key recovery).

**Recommended direction:** retain YAML tooling for literal structure, with a
separate tolerant Jinja lexical layer, original-source spans, explicit unknowns,
and bounded recovery islands. Raw YAML alone fails immediately at ordinary
control blocks. Jinja's syntax parser handles complete template syntax but
rejects incomplete editing states and supplies no YAML declarations. The hybrid
demonstrates useful recovery without needing rendered values. This evidence
justifies the architecture, not shipping this experimental lexer unchanged.

### How this candidate works

1. Parse Jinja syntax only as a comparison; never compile, render, or load a
   template. The candidate's recovery does not depend on syntax parsing success.
2. Recognize template delimiters outside quoted strings/balanced containers.
   Blank control/comment spans and replace expression characters with `x`,
   retaining every newline and character position. Raw-block bodies stay literal.
3. Compose each top-level, unindented YAML island with `SafeLoader`, **without
   constructing Python values**. On YAML failure, recover only complete mapping
   headers (two-space function indentation), marked `partial`.
4. Reject projected keys/references overlapping template spans. Keep source
   headers inside control scopes as `conditional`. Keep dynamic expressions,
   generated IDs, dynamic references, malformed islands and unsupported forms
   in `unknowns`; never use a placeholder as a state ID or path.
5. Record simple literal Jinja import/from/include paths and YAML include items.
   This is extraction only, not resolution, traversal, or availability checking.

The projection is internal analysis data. It is **not** rendered output and must
never be returned as a formatting edit, saved, or used to validate runtime data.

### Source contract

Ranges are half-open Unicode-code-point offsets into the **unchanged original
source**, including quotes when they are part of a YAML scalar token. `line` and
`column` are zero-based code-point coordinates; CRLF is retained. These are not
LSP UTF-16 characters or byte offsets. A generated key has an unknown span, not
an invented value. Function headers under a generated ID remain conditional
source evidence, with no invented parent identifier. Duplicate/branch-local
headers are not merged into one purported runtime declaration.

`literal` means a literal token in source, **not** that the state exists after
rendering. `conditional` does not prove a branch executes or a loop produces an
item. `partial` is a header recovered from a damaged island, not a valid state
body. Unknowns are evidence/status, not built-in error diagnostics.

## Non-execution evidence

`guard.py` fails on template rendering/generation/loading, imports of Salt or
the sentinel workspace module, execution of sentinel project Python, or process
launch attempts. All five evidence fixtures run under those tripwires. Tests
analyze the sentinel template while the working directory contains project code
and a fake Salt command is on `PATH`. No marker is written. Positive controls
explicitly execute the Python and shell sentinels afterwards and verify that
each really writes its marker. Tests remove their temporary marker directory.
The YAML `!!python/object/apply:os.system` fixture is composed, never constructed.

This is repeatable evidence for these code paths, **not an OS sandbox or a proof
about arbitrary future integrations**. Trusted installed parser dependencies
still execute their own Python code. Future integrations must preserve isolated
dependency loading, avoid constructors/imports/rendering, and add equivalent
tripwires at real stdio LSP boundaries.

## Supported boundaries and unresolved cases

- Conventional block-style state mappings; normal nested values and block
  scalars; simple literal references; common if/for blocks and raw blocks.
- Default `jinja|yaml` and explicit `#!jinja|yaml` only. Other renderer shebangs
  produce `unsupported_renderer`, not guessed states (including YAML-only mode,
  which deserves a separate role/renderer path).
- Island boundaries assume unindented top-level source. Header-only error
  recovery assumes two-space function indentation. Top-level flow mappings,
  complex keys, multi-document YAML, unusual indentation, cross-island anchors,
  and environment-qualified include mappings are not proven. Alias failures are
  isolated as malformed YAML; anchors are not shared between islands.
- Whitespace-control semantics, custom Jinja extensions/line statements,
  multiline expressions altering YAML structure, macros/captured output,
  imports that generate YAML, and arbitrary generated fragments are not modeled.
  Control blocks are source scopes, not an execution graph.
- An unclosed delimiter is recovered to the end of its line to retain later
  assistance. This is explicitly unknown, not a claim about how Jinja would
  parse the remaining file. Quoted/escaped reference forms beyond the simple
  literal recognizer require further work; computed paths stay unknown.
- No state-argument/value validation, requisite semantics, include traversal,
  provider resolution, availability checking, or diagnosis of unknown APIs.
- This is a small correctness corpus, not performance/denial-of-service or
  exhaustive malformed-input fuzz testing. Whole-document parser recursion,
  resource limits and cancellation need production hardening.

## Downstream pointers

- Document lifecycle/range work: convert code-point spans centrally to negotiated
  LSP encoding, preserve CRLF/unsaved source, and test actual stdio requests.
- Jinja assistance and outlines: consume literal headers plus uncertainty; do
  not infer generated state IDs, branch outcomes, or runtime argument values.
- Include navigation: extract literal references, then apply configured roots,
  environments, boundaries, cycles and invalidation in the resolver.
- Diagnostics: do not turn `unknowns`, partial recovery, or parser-comparison
  errors into proven Salt errors. Formatting must not reuse this projection.
- Keep this isolated proof as regression evidence when selecting/hardening the
  eventual production analysis implementation.
