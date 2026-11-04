"""
Language Server Protocol implementation
"""

import logging
import os
import re
from functools import wraps
from typing import Dict, List, Optional, Sequence, Tuple, Union, cast

from lsprotocol import types
from lsprotocol.types import (
    TEXT_DOCUMENT_COMPLETION as COMPLETION,
    INITIALIZE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_DEFINITION as DEFINITION,
    TEXT_DOCUMENT_DOCUMENT_SYMBOL as DOCUMENT_SYMBOL,
)
from lsprotocol.types import (
    CompletionItem,
    CompletionList,
    CompletionOptions,
    CompletionParams,
    InitializeParams,
)
from pygls.lsp.server import LanguageServer

from salt_lsp import utils
from salt_lsp.catalog import complete_states, project_catalog
from salt_lsp.custom_sources import discover
from salt_lsp.custom_definitions import definitions
from salt_lsp.project import ProjectModel
from salt_lsp.jinja_calls import complete_execution_calls, hover_execution_call
from salt_lsp.state_calls import complete_arguments, hover_state_call
from salt_lsp.tools import ToolService
from salt_lsp.base_types import StateNameCompletion, SLS_LANGUAGE_ID
from salt_lsp.workspace import SaltLspProto, SlsFileWorkspace
from salt_lsp.parser import (
    IncludesNode,
    RequisiteNode,
    StateParameterNode,
    Tree,
)


class SaltServer(LanguageServer):
    """Experimental language server for salt states"""

    LINE_START_REGEX = re.compile(r"^(\s*)\b", re.MULTILINE)

    def __init__(self) -> None:
        # pygls INFO serializes outbound RPC payloads (including diagnostics,
        # completion documentation and trust tokens). Never log those payloads.
        logging.getLogger("pygls").setLevel(logging.WARNING)
        super().__init__("salt-lsp", "0.0.1", protocol_cls=SaltLspProto)

        self._state_name_completions: Dict[str, StateNameCompletion] = {}

        self.logger: logging.Logger = logging.getLogger()
        self._state_names: List[str] = []
        self.projects = ProjectModel()
        self.exit_process_on_shutdown = False
        self._reporting_error = False
        self.tools = ToolService(
            lambda uri: self.workspace.project_context(uri),
            lambda: [
                *self.projects.project_roots,
                *(utils.FileUri(uri).path for uri in self.workspace.folders),
                *(
                    [utils.FileUri(self.workspace.root_uri).path]
                    if self.workspace.root_uri
                    else []
                ),
            ],
        )

    def report_server_error(self, error, source) -> None:
        """Do not answer a failed send with another send.

        Reporting goes through the same closed stream once the client is gone,
        and without this guard recurses until the interpreter's limit.
        """
        if self._reporting_error:
            return
        self._reporting_error = True
        try:
            super().report_server_error(error, source)
        finally:
            self._reporting_error = False

    def shutdown(self) -> None:
        """Optionally end the process, once the event loop has unwound.

        pygls joins its stdin reader here, which blocks until the client closes
        stdin. A server told to stop must not wait for that.
        """
        if self.exit_process_on_shutdown:
            logging.shutdown()
            os._exit(0)
        super().shutdown()

    @property
    def workspace(self) -> SlsFileWorkspace:
        assert isinstance(super().workspace, SlsFileWorkspace), (
            "expected to get a 'SlsFileWorkspace', but got a "
            f"'{super().workspace.__class__.__name__}' instead"
        )
        return cast(SlsFileWorkspace, super().workspace)

    def post_init(
        self,
        state_name_completions: Dict[str, StateNameCompletion],
        log_level=logging.DEBUG,
    ) -> None:
        """Further initialisation, called after
        setup_salt_server_capabilities."""
        self._state_name_completions = state_name_completions
        self._state_names = list(state_name_completions.keys())
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(log_level)

    def complete_state_name(
        self, params: types.CompletionParams
    ) -> List[Tuple[str, Optional[str]]]:
        """Complete state name at current position"""
        assert (
            params.context is not None
            and params.context.trigger_character == "."
        )

        doc = self.workspace.get_text_document(params.text_document.uri)
        contents = doc.source
        ind = doc.offset_at_position(params.position)
        last_match = utils.get_last_element_of_iterator(
            SaltServer.LINE_START_REGEX.finditer(contents, 0, ind)
        )
        if last_match is None:
            self.logger.debug(
                "expected to find whitespace before the position (%d, %d) "
                "but got no regex match",
                params.position.line,
                params.position.character,
            )
            return []
        state_name = contents[last_match.span()[1] : ind - 1]
        if state_name in self._state_name_completions:
            completer = self._state_name_completions[state_name]
            return completer.provide_subname_completion()
        return []

    def find_id_in_doc_and_includes(
        self, id_to_find: str, starting_uri: str
    ) -> Optional[types.Location]:
        """Finds the first matching location of the given id in the document or
        in its includes.

        This function searches for the `id_to_find` starting in
        `starting_uri`. If it does not find it in there, then it will continue
        to search in the includes and returns the first match that it finds.
        """
        self.logger.debug("Request to find an id from uri '%s'", starting_uri)
        if not utils.is_valid_file_uri(starting_uri):
            return None
        if (tree := self.workspace.trees.get(starting_uri)) is None:
            self.logger.error(
                "Cannot search in '%s', no tree present", starting_uri
            )
            return None

        inc_of_uri = self.workspace.includes.get(starting_uri, [])

        # FIXME: need to take ordering into account:
        # https://docs.saltproject.io/en/latest/ref/states/compiler_ordering.html#the-include-statement
        trees_and_uris_to_search: Sequence[
            Tuple[Tree, Union[str, utils.FileUri]]
        ] = [(tree, starting_uri)] + [
            (t, inc)
            for inc in inc_of_uri
            if (t := self.workspace.trees.get(inc)) is not None
        ]

        for tree, uri in trees_and_uris_to_search:
            self.logger.debug("Searching in '%s'", uri)
            matching_states = [
                state
                for state in tree.states
                if state.identifier == id_to_find
            ]
            if len(matching_states) != 1:
                continue

            if (
                lsp_range := utils.ast_node_to_range(
                    matching_states[0],
                    self.workspace.get_text_document(str(uri)),
                )
            ) is not None:
                self.logger.debug(
                    "found match at '%s', '%s", lsp_range.start, lsp_range.end
                )
                return types.Location(
                    uri=str(uri),
                    range=lsp_range,
                )

        return None


def setup_salt_server_capabilities(server: SaltServer) -> None:
    """Adds the completion, goto definition and document symbol capabilities to
    the provided server.
    """

    @server.feature(INITIALIZE)
    def initialize(params: InitializeParams) -> None:
        """Set up custom workspace."""
        cast(SaltLspProto, server.protocol).setup_custom_workspace()
        server.workspace.projects = server.projects
        options = params.initialization_options
        apply_settings(options if options is not None else {})
        server.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Info,
                message="Salt analysis defaults to Salt 3008 (not a detected runtime).",
            )
        )
        server.logger.debug("Replaced workspace with SlsFileWorkspace")

    @server.feature("salt/projectContext")
    def project_context(params):
        """Expose the selected project/profile to any LSP editor."""
        context = server.workspace.project_context(params.uri)
        return context.to_lsp() if context else None

    @server.feature("salt/toolStatus")
    def tool_status(params):
        return server.tools.inspect(params.uri, params.tool)

    @server.feature("salt/toolProbe")
    async def tool_probe(params):
        return await server.tools.probe(params.uri, params.tool)

    @server.feature("salt/authorizeTool")
    def authorize_tool(params):
        return server.tools.authorize(
            params.uri, params.tool, params.token, params.action
        )

    @server.feature(types.SHUTDOWN)
    async def shutdown_tools(*_args):
        await server.tools.close()

    def project_folders():
        folders = list(server.workspace.folders)
        if not folders and server.workspace.root_uri:
            folders = [server.workspace.root_uri]
        return folders

    def role(uri):
        try:
            return server.projects.context(uri, project_folders()).file_role
        except ValueError:
            return "generic"

    @server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
    def configuration_changed(params: types.DidChangeConfigurationParams):
        """Apply a complete portable settings snapshot and refresh results."""
        previous = {
            uri: server.workspace.project_context(uri)
            for uri in server.workspace.text_documents
        }
        apply_settings(params.settings)
        server.workspace.invalidate_projects(previous)

    def apply_settings(settings):
        if isinstance(settings, dict) and "salt" not in settings:
            return
        salt = settings.get("salt", {}) if isinstance(settings, dict) else None
        if isinstance(salt, dict):
            errors = server.tools.configure(salt.get("tools", []))
            salt = {k: v for k, v in salt.items() if k != "tools"}
        else:
            errors = server.tools.configure([])
        errors += server.projects.configure(
            salt,
            project_folders(),
        )
        for error in errors:
            server.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Error, message=error
                )
            )

    def with_sources(handler):
        @wraps(handler)
        def selected(salt_server, params):
            context = salt_server.workspace.project_context(
                params.text_document.uri
            )
            entries = (
                discover(salt_server.workspace, context) if context else []
            )
            with project_catalog(entries):
                return handler(salt_server, params)

        return selected

    @server.feature(
        COMPLETION,
        CompletionOptions(trigger_characters=["-", ".", "'", '"', "(", ","]),
    )
    @with_sources
    def completions(
        salt_server: SaltServer, params: CompletionParams
    ) -> Optional[CompletionList]:
        """Returns completion items."""
        if not utils.is_valid_file_uri(params.text_document.uri):
            return None
        if role(params.text_document.uri) not in ("state", "state-top"):
            return None
        if (
            params.text_document.uri
            not in salt_server.workspace.text_documents
        ):
            return None
        if role(params.text_document.uri) == "state":
            context = salt_server.workspace.project_context(
                params.text_document.uri
            )
            if context is not None:
                document = salt_server.workspace.get_text_document(
                    params.text_document.uri
                )
                result = complete_execution_calls(
                    document, params.position, context.salt_version
                )
                if result is not None:
                    return result
                result = complete_states(
                    salt_server.workspace.get_text_document(
                        params.text_document.uri
                    ),
                    params.position,
                    context.salt_version,
                )
                if result is None:
                    result = complete_arguments(
                        salt_server.workspace.get_text_document(
                            params.text_document.uri
                        ),
                        params.position,
                        context.salt_version,
                    )
                if result is not None:
                    return result

        if (
            tree := salt_server.workspace.trees.get(params.text_document.uri)
        ) is None:
            return None
        path = utils.construct_path_to_position(
            tree,
            params.position,
            salt_server.workspace.get_text_document(params.text_document.uri),
        )
        if path and (
            isinstance(path[-1], IncludesNode)
            or role(params.text_document.uri) == "state-top"
            and isinstance(path[-1], StateParameterNode)
        ):
            context = salt_server.workspace.project_context(
                params.text_document.uri
            )
            assert context is not None
            includes = set()
            for root in context.state_roots:
                for candidate in root.rglob("*.sls"):
                    selected = salt_server.workspace.project_context(
                        candidate.as_uri()
                    )
                    if (
                        not candidate.resolve().is_relative_to(root)
                        or selected is None
                        or selected.project_root != context.project_root
                        or selected.file_role != "state"
                    ):
                        continue
                    relative = candidate.relative_to(root).with_suffix("")
                    if relative.name == "init":
                        relative = relative.parent
                    name = ".".join(relative.parts)
                    if name:
                        includes.add(name)
            return CompletionList(
                is_incomplete=False,
                items=[
                    CompletionItem(label=f" {include}")
                    for include in sorted(includes)
                ],
            )
        return None

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    @with_sources
    def hover(
        salt_server: SaltServer, params: types.HoverParams
    ) -> Optional[types.Hover]:
        """Document the state call under the cursor from the active catalog."""
        uri = params.text_document.uri
        if not utils.is_valid_file_uri(uri) or role(uri) != "state":
            return None
        context = salt_server.workspace.project_context(uri)
        if context is None or uri not in salt_server.workspace.text_documents:
            return None
        document = salt_server.workspace.get_text_document(uri)
        return hover_execution_call(
            document, params.position, context.salt_version
        ) or hover_state_call(
            salt_server.workspace.get_text_document(uri),
            params.position,
            context.salt_version,
        )

    @server.feature(DEFINITION)
    @with_sources
    def goto_definition(
        salt_server: SaltServer, params: types.DeclarationParams
    ) -> Optional[Union[types.Location, List[types.Location]]]:
        uri = params.text_document.uri
        if not utils.is_valid_file_uri(uri):
            return None
        if role(uri) != "state":
            return None
        context = salt_server.workspace.project_context(uri)
        if context and uri in salt_server.workspace.text_documents:
            locations = definitions(
                salt_server.workspace,
                salt_server.workspace.get_text_document(uri),
                params.position,
                context.salt_version,
            )
            if locations:
                return locations
        if (tree := salt_server.workspace.trees.get(uri)) is None:
            return None
        path = utils.construct_path_to_position(
            tree, params.position, salt_server.workspace.get_text_document(uri)
        )

        # Going to definition is only handled on requisites ids
        if not path or not isinstance(path[-1], RequisiteNode):
            return None

        if (id_to_find := cast(RequisiteNode, path[-1]).reference) is None:
            return None

        return salt_server.find_id_in_doc_and_includes(id_to_find, uri)

    @server.feature(TEXT_DOCUMENT_DID_OPEN)
    def did_open(
        salt_server: SaltServer, params: types.DidOpenTextDocumentParams
    ) -> Optional[types.TextDocumentItem]:
        """Text document did open notification.

        This function registers the newly opened file with the salt server.
        """
        salt_server.logger.debug(
            "adding text document '%s' to the workspace",
            params.text_document.uri,
        )
        return types.TextDocumentItem(
            uri=params.text_document.uri,
            language_id=SLS_LANGUAGE_ID,
            text=params.text_document.text or "",
            version=params.text_document.version,
        )

    @server.feature(DOCUMENT_SYMBOL)
    def document_symbol(
        salt_server: SaltServer, params: types.DocumentSymbolParams
    ) -> Optional[
        Union[List[types.DocumentSymbol], List[types.SymbolInformation]]
    ]:
        if not utils.is_valid_file_uri(params.text_document.uri):
            return []
        if role(params.text_document.uri) != "state":
            return []
        return salt_server.workspace.document_symbols.get(
            params.text_document.uri, []
        )
