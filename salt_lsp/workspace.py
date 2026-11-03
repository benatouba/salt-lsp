"""Module implementing a custom workspace that automatically updates the parsed
contents utilizing the existing Workspace implementation from pygls.

"""

from logging import getLogger, Logger, DEBUG
from pathlib import Path
from typing import List, Optional, Union

from lsprotocol import types
from pygls.protocol import LanguageServerProtocol
from pygls.workspace import Workspace

from salt_lsp.base_types import CompletionsDict, SLS_LANGUAGE_ID
from salt_lsp.utils import UriDict, FileUri, get_top, is_valid_file_uri
from salt_lsp.parser import parse, Tree
from salt_lsp.document_symbols import tree_to_document_symbols


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

        #: top path corresponding to every workspace folder
        self._top_paths: UriDict[Optional[FileUri]] = UriDict()
        self._state_name_completions = state_name_completions

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
        self._includes[text_document_uri] = []
        if (
            (tree := self._trees[text_document_uri]) is None
            or tree.includes is None
            or len(tree.includes.includes) == 0
        ):
            return

        ws_folder = self._get_workspace_of_document(text_document_uri)
        top_path: Union[str, FileUri, None]
        if (
            ws_folder in self._top_paths
            and self._top_paths[ws_folder] is not None
        ):
            top_path = self._top_paths[ws_folder]
        else:
            top_path = ws_folder

        assert top_path is not None

        self._includes[text_document_uri] = [
            FileUri(f)
            for incl in tree.includes.includes
            if (f := incl.get_file(FileUri(top_path).path)) is not None
        ]

        new_includes = self._includes[text_document_uri]

        # now try to re-read all the trees if they are not present:
        for inc in self._includes[text_document_uri]:
            if inc not in self._trees:
                self.logger.debug(
                    "Adding file '%s' via includes of '%s'",
                    inc,
                    text_document_uri,
                )
                with open(inc.path, "r") as inc_file:
                    self.put_text_document(
                        types.TextDocumentItem(
                            uri=str(inc),
                            language_id=SLS_LANGUAGE_ID,
                            version=0,
                            text=inc_file.read(-1),
                        )
                    )
                self._resolve_includes(inc)

            if inc in self._trees and self._includes.get(inc):
                new_includes += self._includes[inc]

        assert len(new_includes) >= len(self._includes[text_document_uri])
        self._includes[text_document_uri] = new_includes

    def _update_document(
        self,
        text_document: Union[
            types.TextDocumentItem, types.VersionedTextDocumentIdentifier
        ],
    ) -> None:
        self.logger.debug("updating document '%s'", text_document.uri)
        uri = text_document.uri
        tree = parse(self.get_text_document(uri).source)
        self._trees[uri] = tree

        self._document_symbols[uri] = tree_to_document_symbols(
            tree, self._state_name_completions, self.get_text_document(uri)
        )

        self._resolve_includes(text_document.uri)

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
        super().add_folder(folder)
        top_path = get_top(FileUri(folder.uri).path)
        self._top_paths[FileUri(folder.uri)] = (
            FileUri(top_path) if top_path is not None else None
        )

    def remove_folder(self, folder_uri: Union[str, FileUri]) -> None:
        super().remove_folder(str(folder_uri))
        self._top_paths.pop(FileUri(folder_uri))

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
        super().remove_text_document(doc_uri)
        self._document_symbols.pop(FileUri(doc_uri), None)
        self._trees.pop(FileUri(doc_uri), None)
        self._includes.pop(FileUri(doc_uri), None)

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
