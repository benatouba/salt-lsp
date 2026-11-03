# Salt Authoring

Vocabulary for understanding Salt source files and the projects that contain them.

## Language

**Project root**:
The boundary of a Salt authoring project, which may contain multiple state roots and pillar roots. It is distinct from either kind of Salt content root.
_Avoid_: State root when referring to the whole project

**State root**:
A directory containing a Salt state tree, within which SLS names identify state files.
_Avoid_: Project root when referring to a state tree

**Pillar root**:
A directory containing a Salt pillar tree. Pillar SLS files describe data rather than state declarations.
_Avoid_: State root when referring to a pillar tree

**Static analysis**:
Analysis of Salt source without rendering templates, importing workspace Python modules, or invoking Salt.
_Avoid_: Dry run, rendered validation

**Rendered validation**:
Validation of data produced by evaluating a Salt render pipeline. Rendering can execute commands and is not inherently side-effect-free.
_Avoid_: Static analysis, safe preview

**API catalog**:
A version-specific collection of known Salt functions, arguments, and documentation. Membership does not establish that a function is available on a particular minion.
_Avoid_: Available functions when referring only to catalog entries
