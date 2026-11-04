"""Non-executing project boundaries and active analysis context.

No Salt configuration, Python, Git commands, or template code is evaluated.
"""

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse


def file_path(uri: str) -> Path:
    """Decode a local file URI without interpreting commands or variables."""
    parsed = urlparse(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in ("", "localhost")
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "expected an absolute local file URI without query or fragment"
        )
    return Path(unquote(parsed.path)).resolve()


@dataclass(frozen=True)
class ProjectContext:
    """Immutable selection consumed by catalog and optional-tool providers."""

    project_root: Path
    active_profile: str = "default"
    salt_version: str = "3008"
    version_source: str = "default"
    environment: str = "base"
    state_roots: tuple[Path, ...] = ()
    pillar_roots: tuple[Path, ...] = ()
    module_dirs: tuple[Path, ...] = ()
    file_roles: tuple[tuple[Path, str], ...] = ()
    file_role: str = "generic"
    revision: int = 0

    def to_lsp(self) -> dict:
        """Return the stable JSON context contract (paths are file URIs)."""
        values = asdict(self)
        return {
            "projectRoot": self.project_root.as_uri(),
            "activeProfile": values["active_profile"],
            "saltVersion": values["salt_version"],
            "versionSource": values["version_source"],
            "environment": values["environment"],
            "stateRoots": [p.as_uri() for p in self.state_roots],
            "pillarRoots": [p.as_uri() for p in self.pillar_roots],
            "moduleDirs": [p.as_uri() for p in self.module_dirs],
            "fileRole": self.file_role,
            "revision": self.revision,
        }


class ProjectModel:
    """Select the nearest Git root, then the deepest editor workspace."""

    def __init__(self) -> None:
        self.revision = 0
        self._projects: list[tuple[Path, ProjectContext]] = []
        self._discovery_profiles: dict[Path, dict] = {}

    @property
    def project_roots(self) -> tuple[Path, ...]:
        """Configured boundaries, including projects outside editor folders.

        Execution selectors also include editor folders and the current
        discovered context. These paths are boundaries, never trust grants.
        """
        return tuple(context.project_root for _, context in self._projects)

    def configure(self, settings: object, folders: Iterable[str]) -> list[str]:
        """Replace explicit settings. Relative paths are literal, not shell text."""
        folders = list(folders)
        errors = []
        try:
            settings = self._object(settings, {"projects"}, "salt")
            entries = settings.get("projects", [])
            if not isinstance(entries, list):
                raise ValueError("salt.projects: expected an array")
        except ValueError as error:
            return [str(error)]
        projects: list[tuple[Path, ProjectContext]] = []
        discovery_profiles = {}
        previous = dict(self._projects)
        for index, entry in enumerate(entries):
            label = f"salt.projects[{index}]"
            scope = None
            try:
                entry = self._object(
                    entry,
                    {"workspaceUri", "root", "activeProfile", "profiles"},
                    label,
                )
                if "workspaceUri" in entry:
                    scope = file_path(
                        self._string(
                            entry["workspaceUri"], label + ".workspaceUri"
                        )
                    )
                elif "root" in entry and (
                    str(entry["root"]).startswith("file:")
                    or Path(str(entry["root"])).is_absolute()
                ):
                    scope = self._path(
                        self._string(entry["root"], label + ".root"), Path("/")
                    )
                elif len(folders) == 1:
                    scope = file_path(folders[0])
                else:
                    raise ValueError(
                        label
                        + ": set workspaceUri or an absolute root in a multi-workspace session"
                    )
                root = self._path(
                    self._string(entry.get("root", "."), label + ".root"),
                    scope,
                )
                active = self._string(
                    entry.get("activeProfile", "default"),
                    label + ".activeProfile",
                )
                profiles = entry.get("profiles", {"default": {}})
                if not isinstance(profiles, dict) or not profiles:
                    raise ValueError(
                        label
                        + ".profiles: expected a nonempty object of named profiles"
                    )
                for name, value in profiles.items():
                    self._string(name, label + ".profiles name")
                    self._validate_profile(value, label + ".profiles." + name)
                if active not in profiles:
                    raise ValueError(
                        label
                        + ".activeProfile: select an existing profile ("
                        + ", ".join(profiles)
                        + ")"
                    )
                profile = profiles[active]
                if scope in dict(projects):
                    raise ValueError(
                        label
                        + ": duplicate project scope; use one entry with named profiles"
                    )
                projects.append(
                    (scope, self._profile_context(root, active, profile))
                )
                if "root" not in entry:
                    discovery_profiles[scope] = profile
            except (ValueError, OSError, RuntimeError) as error:
                errors.append(
                    f"{label}: {error}. Last valid settings retained; correct the settings snapshot."
                )
                if scope is None and index < len(self._projects):
                    scope = self._projects[index][0]
                if scope in previous and scope not in dict(projects):
                    projects.append((scope, previous[scope]))
                    if scope in self._discovery_profiles:
                        discovery_profiles[scope] = self._discovery_profiles[
                            scope
                        ]
        self.revision += 1
        self._projects = [
            (
                scope,
                (
                    previous[scope]
                    if scope in previous
                    and replace(previous[scope], revision=0)
                    == replace(context, revision=0)
                    else replace(context, revision=self.revision)
                ),
            )
            for scope, context in projects
        ]
        self._discovery_profiles = discovery_profiles
        return errors

    def _profile_context(
        self, root: Path, active: str, profile: dict
    ) -> ProjectContext:
        return ProjectContext(
            project_root=root,
            active_profile=active,
            salt_version=profile.get("saltVersion", "3008"),
            version_source=(
                "configured" if "saltVersion" in profile else "default"
            ),
            environment=profile.get("environment", "base"),
            state_roots=tuple(
                self._path(p, root) for p in profile.get("stateRoots", [])
            ),
            pillar_roots=tuple(
                self._path(p, root) for p in profile.get("pillarRoots", [])
            ),
            module_dirs=tuple(
                self._path(p, root) for p in profile.get("moduleDirs", [])
            ),
            file_roles=tuple(
                (self._path(p, root), role)
                for p, role in profile.get("fileRoles", {}).items()
            ),
        )

    @staticmethod
    def _object(value: object, keys: set[str], label: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError(label + ": expected an object")
        unknown = value.keys() - keys
        if unknown:
            raise ValueError(
                label + ": unknown setting " + ", ".join(map(str, unknown))
            )
        return value

    @staticmethod
    def _string(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(
                label + ": expected a nonempty string without NUL"
            )
        return value

    def _validate_profile(self, value: object, label: str) -> None:
        profile = self._object(
            value,
            {
                "saltVersion",
                "environment",
                "stateRoots",
                "pillarRoots",
                "moduleDirs",
                "fileRoles",
            },
            label,
        )
        if profile.get("saltVersion", "3008") not in ("3006", "3007", "3008"):
            raise ValueError(
                label
                + '.saltVersion: expected "3006", "3007", or "3008" (a string)'
            )
        self._string(
            profile.get("environment", "base"), label + ".environment"
        )
        for key in ("stateRoots", "pillarRoots", "moduleDirs"):
            paths = profile.get(key, [])
            if not isinstance(paths, list):
                raise ValueError(
                    label + "." + key + ": expected an array of literal paths"
                )
            for path in paths:
                self._path(self._string(path, label + "." + key), Path("/"))
        roles = profile.get("fileRoles", {})
        if not isinstance(roles, dict):
            raise ValueError(
                label
                + ".fileRoles: expected an object of exact paths to roles"
            )
        for path, role in roles.items():
            self._path(
                self._string(path, label + ".fileRoles path"), Path("/")
            )
            if role not in (
                "state",
                "pillar",
                "state-top",
                "pillar-top",
                "generic",
            ):
                raise ValueError(
                    label
                    + ".fileRoles: role must be state, pillar, state-top, pillar-top, or generic"
                )

    @staticmethod
    def _path(value: str, root: Path) -> Path:
        if value.startswith("file:"):
            return file_path(value)
        if urlparse(value).scheme:
            raise ValueError(
                "paths must be local paths or file URIs, not remote URIs"
            )
        return (root / value).resolve()

    def context(self, uri: str, folders: Iterable[str]) -> ProjectContext:
        """Resolve a document without reading or executing project content."""
        path = file_path(uri)
        folders = list(folders)
        for scope, context in sorted(
            self._projects, key=lambda p: len(p[0].parts), reverse=True
        ):
            if path.is_relative_to(scope) or path.is_relative_to(
                context.project_root
            ):
                if scope in self._discovery_profiles:
                    context = replace(
                        self._profile_context(
                            self._discover(path, folders),
                            context.active_profile,
                            self._discovery_profiles[scope],
                        ),
                        revision=context.revision,
                    )
                return replace(context, file_role=self._role(path, context))
        # Explicit external sources retain their owning project. Shared sources
        # have no unique active target; do not choose a profile by list order.
        external = [
            context
            for scope, context in self._projects
            if scope not in self._discovery_profiles
            and (
                any(
                    path.is_relative_to(root)
                    for root in (
                        *context.state_roots,
                        *context.pillar_roots,
                        *context.module_dirs,
                    )
                )
                or path in dict(context.file_roles)
            )
        ]
        if len(external) == 1:
            return replace(
                external[0], file_role=self._role(path, external[0])
            )
        return ProjectContext(self._discover(path, folders))

    @staticmethod
    def _discover(path: Path, folders: Iterable[str]) -> Path:
        workspace = sorted(
            (file_path(folder) for folder in folders),
            key=lambda folder: len(folder.parts),
            reverse=True,
        )
        fallback = next(
            (p for p in workspace if path.is_relative_to(p)), path.parent
        )
        root = fallback
        for parent in (path.parent, *path.parent.parents):
            marker = parent / ".git"
            if marker.is_dir() or marker.is_file():
                root = parent
                break
        return root

    @staticmethod
    def _role(path: Path, context: ProjectContext) -> str:
        overrides = dict(context.file_roles)
        if path in overrides:
            return overrides[path]
        if path.suffix != ".sls":
            return "generic"
        state = any(path.is_relative_to(root) for root in context.state_roots)
        pillar = any(
            path.is_relative_to(root) for root in context.pillar_roots
        )
        if state == pillar:
            return "generic"
        role = "state" if state else "pillar"
        return role + "-top" if path.name == "top.sls" else role
