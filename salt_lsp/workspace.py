"""Module implementing a custom workspace that automatically updates the parsed
contents utilizing the existing Workspace implementation from pygls.

"""

from logging import getLogger, Logger, DEBUG
from pathlib import Path
from typing import List, Optional, Union

from lsprotocol import types
from pygls.protocol import LanguageServerProtocol
from pygls.workspace import Workspace

from salt_lsp.base_types import CompletionsDict
from salt_lsp.utils import UriDict, FileUri, is_valid_file_uri
from salt_lsp.parser import parse, Tree
from salt_lsp.document_symbols import tree_to_document_symbols
from salt_lsp.source_analysis import source_tree
from salt_lsp.project import ProjectModel, ProjectContext
from salt_lsp.source_references import SourceView


class SlsFileWorkspace(Workspace):
    """An extension of pygl's :ref:`Workspace` class that has additional
    properties that are collected from the workspace.

    It hooks into the :ref:`Workspace`'s update function to automatically keep
    all properties up to date.
    """

    def __init__(
        self, state_name_completions: CompletionsDict, *args, **kwargs
    ) -> None:
        #: dictionary containing the parsed contents of all tracked documents
        self._trees: UriDict[Tree] = UriDict()

        #: document symbols of all tracked documents
        self._document_symbols: UriDict[List[types.DocumentSymbol]] = UriDict()

        #: included FileUris of every tracked document
        self._includes: UriDict[List[FileUri]] = UriDict()

        self._state_name_completions = state_name_completions
        self.projects = ProjectModel()
        self._document_sequence = 0
        self._document_stamps: dict[str, tuple[int, int | None]] = {}
        self.on_change = lambda uri: None

        self.logger: Logger = getLogger(self.__class__.__name__)
        # FIXME: make this configurable
        self.logger.setLevel(DEBUG)

        super().__init__(*args, **kwargs)

    @property
    def trees(self) -> UriDict[Tree]:
        """A dictionary which contains the parsed :ref:`Tree` for each document
        tracked by the workspace.
        """
        return self._trees

    @property
    def document_symbols(self) -> UriDict[List[types.DocumentSymbol]]:
        """The document symbols of each SLS files in the workspace."""
        return self._document_symbols

    @property
    def includes(self) -> UriDict[List[FileUri]]:
        """The list of includes of each SLS file in the workspace."""
        return self._includes

    def _resolve_includes(
        self, text_document_uri: Union[str, FileUri]
    ) -> None:
        context = self.project_context(str(text_document_uri))
        self._includes[text_document_uri] = []
        if context is not None and context.file_role in ("state", "pillar"):
            origin = Path(FileUri(text_document_uri).path).resolve()
            self._includes[text_document_uri] = [
                FileUri(path.as_uri())
                for path, _ in SourceView(self, context).closure(origin)
                if path != origin
            ]

    def project_context(self, uri: str) -> Optional[ProjectContext]:
        """Public selection seam shared by language providers."""
        folders = list(self.folders)
        if not folders and self.root_uri:
            folders = [self.root_uri]
        try:
            return self.projects.context(uri, folders)
        except ValueError:
            return None

    def document_stamp(self, uri: str) -> tuple[int, int | None] | None:
        """Opaque lifetime/version stamp for async tool providers.

        Unlike an LSP version integer this cannot match a close/reopen or an
        accepted same-version update. None means no live tracked document.
        Capture this plus project_context before work; compare before publishing.
        """
        return self._document_stamps.get(uri)

    def invalidate_projects(self, previous: dict) -> None:
        """Refresh only documents whose immutable analysis context changed."""
        changed = [
            uri
            for uri in self.text_documents
            if previous.get(uri) != self.project_context(uri)
        ]
        for uri in changed:
            self._trees.pop(FileUri(uri), None)
            self._document_symbols.pop(FileUri(uri), None)
            self._includes.pop(FileUri(uri), None)
        for uri in changed:
            document = self.get_text_document(uri)
            self._update_document(
                types.VersionedTextDocumentIdentifier(
                    uri=uri, version=document.version or 0
                )
            )

    def _update_document(
        self,
        text_document: Union[
            types.TextDocumentItem, types.VersionedTextDocumentIdentifier
        ],
    ) -> None:
        self.logger.debug("updating document '%s'", text_document.uri)
        uri = text_document.uri
        self._document_sequence += 1
        self._document_stamps[uri] = (
            self._document_sequence,
            self.get_text_document(uri).version,
        )
        tree = parse(self.get_text_document(uri).source)
        self._trees[uri] = tree

        self._document_symbols[uri] = tree_to_document_symbols(
            source_tree(self.get_text_document(uri).source),
            self._state_name_completions,
            self.get_text_document(uri),
        )

        self._resolve_includes(text_document.uri)
        self.on_change(str(uri))

    def _get_workspace_of_document(
        self, uri: Union[str, FileUri]
    ) -> Union[str, FileUri]:
        for workspace_uri in self._folders:

            if Path(FileUri(uri).path).is_relative_to(
                Path(FileUri(workspace_uri).path)
            ):
                return workspace_uri

        return self.root_uri

    def add_folder(self, folder: types.WorkspaceFolder) -> None:
        previous = {
            uri: self.project_context(uri) for uri in self.text_documents
        }
        super().add_folder(folder)
        self.invalidate_projects(previous)

    def remove_folder(self, folder_uri: Union[str, FileUri]) -> None:
        previous = {
            uri: self.project_context(uri) for uri in self.text_documents
        }
        super().remove_folder(str(folder_uri))
        self.invalidate_projects(previous)

    def update_text_document(
        self,
        text_document: types.VersionedTextDocumentIdentifier,
        change: types.TextDocumentContentChangeEvent,
    ) -> None:
        if not is_valid_file_uri(text_document.uri):
            return
        current = self.text_documents.get(text_document.uri)
        if current is None or (
            current.version is not None
            and text_document.version < current.version
        ):
            return
        super().update_text_document(text_document, change)
        self._update_document(text_document)

    def remove_text_document(self, doc_uri: str) -> None:
        if not is_valid_file_uri(doc_uri):
            return
        self._document_stamps.pop(doc_uri, None)
        super().remove_text_document(doc_uri)
        self._document_symbols.pop(FileUri(doc_uri), None)
        self._trees.pop(FileUri(doc_uri), None)
        self._includes.pop(FileUri(doc_uri), None)
        self.on_change(doc_uri)

    def put_text_document(
        self, text_document: types.TextDocumentItem, notebook_uri=None
    ) -> None:
        if not is_valid_file_uri(text_document.uri):
            return
        super().put_text_document(text_document, notebook_uri=notebook_uri)
        self._update_document(text_document)


class SaltLspProto(LanguageServerProtocol):
    """Custom protocol that replaces the workspace with a SlsFileWorkspace
    instance.
    """

    workspace: SlsFileWorkspace

    def setup_custom_workspace(self):
        """Replace self.workspace with an instance of SlsFileWorkspace

        This function is only supposed to be called when handling an
        INITIALIZE request.
        """
        if not isinstance(self.workspace, SlsFileWorkspace):
            old_ws = self.workspace
            self._workspace = SlsFileWorkspace(
                self._server._state_name_completions,
                old_ws.root_uri,
                self._server._text_document_sync_kind,
                old_ws.folders.values(),
                old_ws.position_encoding,
            )
