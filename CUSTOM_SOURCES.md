# Custom Salt source declarations

The active profile's state roots supply immediate `_states/*.py` and
`_modules/*.py` sources. `moduleDirs` supplies additional immediate execution
module sources. The first state root containing the same kind/filename wins;
additional directories do not imply a provider priority. Sources belonging to
another project are excluded, as are escaping symlinks and private modules.

Every assistance request inspects current source, using open buffers instead of
disk even when empty or invalid. Closing restores current disk content; deletion
removes the declaration. Invalid Python contributes no last-good metadata.
Discovery is bounded to 2048 files and 1 MiB per source. Packages and dynamic
factories are not interpreted. Defaults, annotations, decorators, imports and
virtual hooks are never evaluated. A declaration is not runtime availability.

Signatures retain positional-only, keyword-only, variadic parameters and source
expressions for defaults and annotations. Nested functions and class methods
are not exports. Standard Python tooling retains generic Python completion,
diagnostics and formatting; Salt LSP only consumes source for Salt features.
# Static exports and navigation

Literal `__virtualname__` (with a `__virtual__` hook), literal hook return
names, `__virtual_aliases__`, and `__func_alias__` supply authoring names.
Hooks, conditional branches, decorators, defaults and annotations are never run.
`__load__` takes precedence over `__all__`, including empty lists. Private
attributes are filtered before function aliases. Assignment aliases retain the
function declaration bound at that point. Dynamic restrictions remain unresolved;
otherwise discoverable declarations are retained, not certified runtime exports.
Conditional declarations and conflicting providers remain candidates.

Only identical source filenames in ordered roots shadow lower providers (including
packaged providers); sharing a virtual namespace alone never establishes priority.
Standard definition requests on state and literal Jinja calls return current Python
declaration locations, including multiple candidates. Python documents remain owned
by the user's Python tooling: no Salt Python completion/hover/formatting is installed.
