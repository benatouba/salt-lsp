"""
Utility functions to extract data from the files
"""

from __future__ import annotations

from collections.abc import MutableMapping
import os
import os.path
import re
import shlex
import subprocess
from typing import (
    Dict,
    Generic,
    Iterator,
    List,
    NewType,
    Optional,
    TypeVar,
    Union,
)
from urllib.parse import urlparse, unquote, quote

from lsprotocol.types import Position, Range
from pygls.workspace import TextDocument

from salt_lsp import parser
from salt_lsp.parser import AstNode, Tree


def get_git_root(path: str) -> Optional[str]:
    """Get the root of the git repository to which `path` belongs.

    If git is not installed or `path` is not in a git repository, then `None`
    is returned.
    """
    res = subprocess.run(
        shlex.split("git rev-parse --show-toplevel"),
        cwd=os.path.dirname(path) if not os.path.isdir(path) else path,
        check=False,
        capture_output=True,
    )
    if res.returncode == 0:
        return str(res.stdout.strip(), encoding="utf-8")
    else:
        return None


def get_top(path: str) -> Optional[str]:
    if os.path.isdir(path):
        if os.path.isfile(os.path.join(path, "top.sls")):
            return path

    parent, tail = os.path.split(path)
    if (tail == "" and parent == "/") or not parent:
        return None
    if os.path.isfile(os.path.join(parent, "top.sls")):
        return parent
    return get_top(parent)


def get_root(path: str) -> Optional[str]:
    root = get_top(path)
    return root or get_git_root(path)


def get_sls_includes(path: str) -> List[str]:
    sls_files = []
    top = get_root(path)
    if not top:
        return []
    for root, _, files in os.walk(top):
        base = root[len(top) + 1 :].replace(os.path.sep, ".")
        sls_files += [
            base + (file[:-4] if file != "init.sls" else "")
            for file in files
            if file.endswith(".sls")
        ]
    return sls_files


def construct_path_to_position(
    tree: Tree, pos: Position, document: Optional[TextDocument] = None
) -> List[AstNode]:
    """Find cursor context, decoding negotiated units when given a document."""
    if document is not None and 0 <= pos.line < len(document.lines):
        pos = document.position_codec.position_from_client_units(
            document.lines, Position(pos.line, pos.character)
        )
    found_node = None
    parser_pos = parser.Position(line=pos.line, col=pos.character)

    def visitor(node: AstNode) -> bool:
        if (
            node.start is not None
            and node.start <= parser_pos
            and (node.end is None or parser_pos <= node.end)
        ):
            nonlocal found_node
            found_node = node
        return True

    tree.visit(visitor)

    if not found_node:
        return []

    context: List[AstNode] = []
    node: Optional[AstNode] = found_node
    while node:
        context.insert(0, node)
        node = node.parent
    return context


def position_to_index(text: str, line: int, column: int) -> int:
    split = text.splitlines(keepends=True)
    return sum([len(l) for i, l in enumerate(split) if i < line]) + column


T = TypeVar("T")


def get_last_element_of_iterator(iterator: Iterator[T]) -> Optional[T]:
    """
    Returns the last element of from an iterator or None if the iterator is
    empty.
    """
    try:
        *_, last = iterator
        return last
    except ValueError:
        # empty iterator
        return None


#: Type for URIs
Uri = NewType("Uri", str)


class FileUri:
    """Canonical file URI and decoded path, including drive and UNC forms.

    Filesystem paths are quoted, not URL-parsed; encoded URI paths are decoded
    exactly once. Drive URI paths retain their leading slash on POSIX hosts.
    """

    _uri: str
    _path: str

    def __init__(self, uri: Union[str, Uri, FileUri]) -> None:
        if isinstance(uri, FileUri):
            self._uri, self._path = uri._uri, uri._path
            return
        # Paths are not URLs: literal %, # and ? must survive a round trip.
        drive_path = bool(re.match(r"^[A-Za-z]:[\\/]", uri))
        parsed = urlparse(uri) if not drive_path else None
        authority = ""
        if parsed is not None and parsed.scheme:
            if parsed.scheme != "file":
                raise ValueError(f"Invalid uri scheme {parsed.scheme}")
            if parsed.query or parsed.fragment:
                raise ValueError(
                    "File URIs cannot contain queries or fragments"
                )
            authority = parsed.netloc
            if authority.lower() == "localhost":
                authority = ""
            path = unquote(parsed.path)
        else:
            path = (
                uri.replace("\\", "/")
                if drive_path or uri.startswith("\\\\")
                else uri
            )
            if path.startswith("//"):
                authority, _, tail = path[2:].partition("/")
                path = "/" + tail
            elif drive_path:
                path = "/" + path
            elif not path.startswith("/"):
                path = os.path.abspath(path)
        if "\x00" in path or "\x00" in authority:
            raise ValueError("NUL in file URI")
        path = path or "/"
        self._uri = "file://" + authority + quote(path, safe="/:")
        self._path = ("//" + authority if authority else "") + path
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", self._path):
            self._path = self._path[1:]

    @property
    def path(self) -> str:
        return self._path

    def __str__(self) -> str:
        return self._uri


U = Union[Uri, FileUri, str]


class UriDict(Generic[T], MutableMapping):
    """Dictionary that stores elements assigned to paths which are then
    transparently accessible via their Uri or the path or the FileUri.
    """

    def __init__(self, *args, **kwargs):
        self._data: Dict[str, T] = {}
        self.update(dict(*args, **kwargs))

    def __getitem__(self, key: U) -> T:
        return self._data[self._key_gen(key)]

    def __setitem__(self, key: U, value: T) -> None:
        self._data[self._key_gen(key)] = value

    def __delitem__(self, key: U) -> None:
        del self._data[self._key_gen(key)]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def _key_gen(self, key: U) -> str:
        return str(FileUri(key))


def is_valid_file_uri(uri: str) -> bool:
    """Returns True if uri is a valid file:// URI"""
    try:
        FileUri(uri)
        return True
    except ValueError:
        return False


def ast_node_to_range(
    node: AstNode, document: Optional[TextDocument] = None
) -> Optional[Range]:
    """
    Converts a AstNode to a Range spanning from the node's starts to its end.

    If the node's start or end are None, then None is returned.
    Pass the target document at LSP boundaries to encode character units.
    Without a document, the range retains parser (Unicode codepoint) units.
    """
    if node.start is None or node.end is None:
        return None
    result = Range(start=node.start.to_lsp_pos(), end=node.end.to_lsp_pos())
    return (
        document.position_codec.range_to_client_units(document.lines, result)
        if document is not None
        else result
    )
