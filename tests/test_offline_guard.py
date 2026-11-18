"""Static checks: the package talks only to local adb and only creates new files.

Network and process rules: no network library anywhere, `socket` and
`subprocess` only in adb.py, no import by name, no process started through `os`.

Rules for files (an allowlist, not a denylist):

uiprobe's code (src/uiprobe) must never delete, rename, truncate or overwrite
a file. Today it creates no file at all. Instead of listing what is
dangerous after the fact, this check flags every REFERENCE (a call, but also
`writer = open`, `opener=open`, `getattr(os, "replace")`) to a name that can
open or change a file, and every import of a module that is not on the
APPROVED_IMPORTS list. What stays is allowed only through ALLOWED_CALLS below.

What is flagged (see `file_changes`):
  * the builtin `open`, and the names in FLAGGED_NAMES (`io.open`,
    `os.open`, `os.remove`, `os.replace`, `os.ftruncate`, ...), through any
    `import x as y` or `from x import y` alias;
  * the `Path` and writer methods in PATH_METHODS, matched by name on any
    receiver (a false positive on an unrelated method is accepted and goes
    into ALLOWED_CALLS), and `copy` when it is given an argument (`dict.copy()`
    passes, `path.copy(target)` does not);
  * any import (``import x``, ``from x import y``, any submodule) of a module
    that is not in APPROVED_IMPORTS, stdlib or not; relative imports and
    ``uiprobe`` itself are fine (see below);
  * a bare use of `os`, `io`, `builtins`, ... as a value (`x = os`,
    `f(os)`, `from os import *`), `__import__`, `eval`, `exec`, `globals`;
  * `getattr`/`setattr`/`delattr`/`__import__`/`import_module` calls with a
    string that names one of the above (or, for getattr, a name that is not
    a literal).

How to approve a module: add its full dotted name to APPROVED_IMPORTS with a
reason that says why it cannot write a file except through the names flagged
here (FLAGGED_NAMES, PATH_METHODS). Approve only a module you have read about:
the writers in NEVER_APPROVE (shutil, tempfile, cProfile, pstats, wave, zipfile,
...) write files through ordinary-looking calls such as ``dump_stats(path)``
and must stay out. A submodule needs its own entry
(``xml.etree.ElementTree``); ``from x import y`` needs ``x``, and ``x.y`` as
well when ``y`` is a submodule. Add a regression vector to
`test_file_changes_are_found` or `test_a_never_approved_import_is_flagged`.

How to allow a call site: add an entry to ALLOWED_CALLS, keyed by the path
relative to src/uiprobe (`cli.py`, `sub/cli.py`), holding the call exactly as
`ast.unparse` prints it (so quotes, spacing and argument order must match;
an alias, an extra argument or other flags no longer match). The entry
exempts only that call's function reference; its arguments are checked as
usual. A future file-creating call must create and never replace, for example:
    "store.py": {"os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)"}
(or `open(path, "x")`), and the same change must show up in review. A stale
entry (one that no longer matches any call) fails `test_allowed_calls_are_used`.

Known limits of an AST check, accepted: `sys.modules["os"].remove`,
`os.__dict__["remove"]`, re-exports such as `subprocess.os.remove`, and similar
deliberate misuse (`io._io.open` is caught: `open` is matched by name). The
runtime guard in conftest.py judges deletes, renames and truncates, and also
blocks write-mode opens inside the repository.
"""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "src" / "uiprobe"
SOURCES = sorted(SOURCE_DIR.rglob("*.py"))

# A module is forbidden when its dotted name equals an entry or lies under it.
FORBIDDEN_MODULES = frozenset(
    {
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "telnetlib",
        "xmlrpc",
        "ssl",
        "_ssl",
        "socketserver",
        "webbrowser",
        "asyncio",
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "logging.handlers",  # HTTPHandler and SysLogHandler open sockets
        "multiprocessing",  # connections, managers, process pools
        "concurrent.futures.process",
        "pty",
        "ctypes",  # could call anything
        "importlib",  # could import any of the above by name
        "runpy",
        "pickle",  # loading one runs code
    }
)
ADB_ONLY_MODULES = frozenset({"socket", "_socket", "subprocess"})
ADB_MODULE = "adb.py"

DYNAMIC_IMPORTS = frozenset({"__import__", "import_module"})
# `os` functions that start a process or replace this one.
PROCESS_FUNCTIONS = frozenset({"system", "popen", "startfile", "fork", "forkpty"})
PROCESS_PREFIXES = ("exec", "spawn", "posix_spawn")


def imported_modules(source: str) -> set[str]:
    """Return the dotted name of every module the source imports.

    ``from a import b`` counts both ``a`` and ``a.b``, since ``b`` may be a
    submodule.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def is_under(module: str, banned: frozenset[str]) -> bool:
    """Return True when `module` is, or is inside, one of the `banned` modules."""
    parts = module.split(".")
    return any(".".join(parts[:i]) in banned for i in range(1, len(parts) + 1))


def banned_imports(source: str, banned: frozenset[str]) -> list[str]:
    return sorted(m for m in imported_modules(source) if is_under(m, banned))


def _is_process_function(name: str) -> bool:
    return name in PROCESS_FUNCTIONS or name.startswith(PROCESS_PREFIXES)


def dynamic_imports(source: str) -> list[int]:
    """Return the lines that import by name, such as ``__import__(...)``."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if isinstance(func, ast.Attribute):
            name = func.attr
        if name in DYNAMIC_IMPORTS:
            lines.append(node.lineno)
    return lines


def _starts_process(node: ast.Attribute | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and _is_process_function(node.attr)
        )
    return node.module == "os" and any(
        _is_process_function(alias.name) for alias in node.names
    )


def process_starters(source: str) -> list[int]:
    """Return the lines that use an ``os`` function that starts a process."""
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.ImportFrom) and _starts_process(node)
    ]


# The only modules src/uiprobe may import (relative imports and `uiprobe.*`
# aside): full dotted name -> why it cannot write a file except through names
# this check already flags. Anything else is flagged, whatever it is.
APPROVED_IMPORTS: dict[str, str] = {
    "__future__": "compiler flags only",
    "argparse": "reads arguments; FileType is flagged, output goes to given streams",
    "collections.abc": "abstract base classes",
    "contextlib": "context-manager helpers; no file access",
    "dataclasses": "class generation; no file access",
    "datetime": "dates and times",
    "errno": "error number constants",
    "hashlib": "digests of bytes already read",
    "io": "its openers (open, FileIO, open_code) are flagged; BytesIO is memory",
    "ipaddress": "address parsing",
    "json": "dump() writes only to a stream that something else opened",
    "os": "every opener, writer and remover is in FLAGGED_NAMES; bare use is flagged",
    "pathlib": "its writers are in PATH_METHODS and matched on any receiver",
    "re": "regular expressions",
    "secrets": "random values",
    "selectors": "waits on sockets that are already open; no file access",
    "shlex": "string quoting",
    "socket": "network, not files (adb.py only, see the test of that name)",
    "stat": "mode constants",
    "subprocess": "starts processes, not files (adb.py only, and the process guard)",
    "sys": (
        "streams; .write/.truncate are flagged on any receiver (sys.stdout.write "
        "too), but print(..., file=sys.stdout) is not flagged and only reaches "
        "streams that are already open"
    ),
    "time": "clocks",
    "typing": "annotations",
    "unicodedata": "character tables",
    "xml.etree.ElementTree": "parses text; write() and write_c14n() are flagged",
}
# Never approve these: why each one writes or deletes files through calls this
# check cannot tell from reading (a path or a name goes in, a file comes out).
NEVER_APPROVE: dict[str, str] = {
    "shutil": "copies, moves and deletes trees",
    "tempfile": "creates and removes files and directories",
    "zipfile": "writes and extracts archives",
    "tarfile": "writes and extracts archives",
    "gzip": "GzipFile writes a file by name",
    "bz2": "BZ2File writes a file by name",
    "lzma": "LZMAFile writes a file by name",
    "logging": "file handlers",
    "mmap": "writes through a mapping",
    "fileinput": "in-place editing",
    "dbm": "opens a database file for writing",
    "shelve": "opens a database file for writing",
    "sqlite3": "creates and writes database files",
    "cProfile": "Profile().dump_stats(path) writes a file",
    "profile": "dump_stats(path) writes a file",
    "pstats": "Stats().dump_stats(path) writes a file",
    "wave": "open(path, 'wb') writes a file",
    "aifc": "open(path, 'wb') writes a file",
    "sunau": "open(path, 'wb') writes a file",
    "pickle": "dump writes to a stream; loading runs code",
    "marshal": "dump writes to a stream",
    "csv": "writers write to streams, often a file opened elsewhere",
    "configparser": "ConfigParser.write(fp) writes to streams",
    "plistlib": "dump(value, fp) writes to streams",
    "trace": "writes coverage files",
    "py_compile": "compile() writes a .pyc",
    "compileall": "writes .pyc files",
    "zipapp": "creates archives",
    "posix": "os under its other name",
    "nt": "os under its other name",
    "_io": "io under its other name",
}
# Modules whose attributes open or change files: using the module itself as a
# value would hide the attribute access from this check.
FILE_ACCESS_MODULES = frozenset(
    {"os", "io", "builtins", "codecs", "posix", "nt", "_io", "argparse"}
)
FLAGGED_NAMES = frozenset(
    {
        "open",  # the builtin
        "builtins.open",
        "codecs.open",
        "io.open",
        "io.FileIO",
        "io.open_code",
        "pathlib.Path.open",
        "pathlib.Path.replace",
        "os.open",
        "os.fdopen",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.renames",
        "os.replace",
        "os.rmdir",
        "os.removedirs",
        "os.truncate",
        "os.ftruncate",
        "os.link",
        "os.symlink",
        "os.mkfifo",
        "os.mknod",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.mkdir",
        "os.makedirs",
        "os.lchmod",
        "os.lchown",
        "os.fchmod",
        "os.fchown",
        "os.posix_fallocate",
        "os.copy_file_range",
        "os.write",
        "os.writev",
        "os.pwrite",
        "os.pwritev",
        "os.eventfd_write",
        "os.sendfile",
        "os.splice",
        "os.setxattr",
        "os.removexattr",
        "os.chflags",
        "os.lchflags",
        "argparse.FileType",  # opens (and truncates) the file it is given
    }
)
# Matched by name on any receiver: the type of the receiver is not known here.
PATH_METHODS = frozenset(
    {
        "open",
        "write_text",
        "write_bytes",
        "touch",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "mkdir",
        "symlink_to",
        "hardlink_to",
        "link_to",
        "chmod",
        "lchmod",
        "move",
        "move_into",
        "copy_into",  # Python 3.14: overwrites an existing target
        "write",  # file objects and ElementTree.write
        "writelines",
        "write_c14n",  # ElementTree.write_c14n
        "truncate",  # file objects and streams: `stream.seek(0); stream.truncate(0)`
    }
)
# Flagged only when called with an argument or used as a value: `dict.copy()`
# takes none, `Path.copy(target)` (Python 3.14) overwrites `target`.
ARGUMENT_METHODS = frozenset({"copy"})
# Names that reach the above by a string or hide a module, never needed here.
DYNAMIC_NAMES = frozenset({"__import__", "__builtins__", "eval", "exec", "globals"})
STRING_LOOKUPS = frozenset(
    {"getattr", "setattr", "delattr", "__import__", "import_module", "__getattribute__"}
)
_FLAGGED_LEAVES = frozenset(name.rsplit(".", 1)[-1] for name in FLAGGED_NAMES)

# The permitted call sites: file name in src/uiprobe -> calls as `ast.unparse`
# prints them. Each has a reason; keep the list short.
ALLOWED_CALLS: dict[str, frozenset[str]] = {
    # Discard output after a broken pipe: only the null device, write-only.
    "cli.py": frozenset({"os.open(os.devnull, os.O_WRONLY)"}),
    # Not files: `datetime.replace` and `bytes.replace`.
    "device.py": frozenset({"datetime.now(UTC).astimezone().replace(microsecond=0)"}),
    # Not files: `bytes.replace`, and `getattr(element, field)` in `find`, which
    # reads the text, resource_id or content_desc attribute of a UI element
    # (`field` is one of those three literals, never a file name).
    "model.py": frozenset({"raw.replace(b'\\x00', b'')", "getattr(element, field)"}),
}


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Map every name an import binds to the qualified name it stands for."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:  # `import os.path` binds `os`
                    top = alias.name.split(".", 1)[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Return the dotted name of a name or attribute chain, aliases resolved."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value, aliases)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _is_own_module(name: str) -> bool:
    return name == "uiprobe" or name.startswith("uiprobe.")


def _is_submodule(name: str) -> bool:
    """Return True when `name` (``pkg.x``) is a module, not an attribute of pkg.

    Only asked after ``pkg`` is approved, so importing it here runs approved
    code only.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _unapproved_module(name: str) -> bool:
    return not _is_own_module(name) and name not in APPROVED_IMPORTS


def _flagged_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(_unapproved_module(alias.name) for alias in node.names)
    if node.level or not node.module:
        return False  # a relative import names uiprobe's own modules
    module = node.module
    if _unapproved_module(module):
        return True
    return any(
        f"{module}.{alias.name}" in FLAGGED_NAMES
        or (alias.name == "*" and module in FILE_ACCESS_MODULES)
        or (
            not _is_own_module(module)
            and _unapproved_module(f"{module}.{alias.name}")
            and _is_submodule(f"{module}.{alias.name}")
        )
        for alias in node.names
    )


def _names_something_flagged(text: str) -> bool:
    parts = set(text.split("."))
    return bool(
        parts & (frozenset(NEVER_APPROVE) | FILE_ACCESS_MODULES | _FLAGGED_LEAVES)
        or parts & PATH_METHODS
    )


def _is_flagged_string_lookup(call: ast.Call) -> bool:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name not in STRING_LOOKUPS:
        return False
    arguments = [*call.args, *(k.value for k in call.keywords)]
    if name in {"getattr", "setattr", "delattr"}:
        attribute = call.args[1] if len(call.args) > 1 else None
        if not (
            isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)
        ):
            return True  # a computed name cannot be shown to be harmless
    return any(
        isinstance(a, ast.Constant)
        and isinstance(a.value, str)
        and _names_something_flagged(a.value)
        for a in arguments
    )


def _allowed_functions(tree: ast.AST, filename: str, allowed: dict) -> set[ast.AST]:
    """Return the function nodes of the calls listed in `allowed` for `filename`."""
    calls = allowed.get(filename, frozenset())
    return {
        node.func
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node) in calls
    }


def _used_with_argument(node: ast.Attribute, parents: dict) -> bool:
    """Return False only for a call of the method that passes no argument."""
    parent = parents.get(node)
    if isinstance(parent, ast.Call) and parent.func is node:
        return bool(parent.args or parent.keywords)
    return True


def file_changes(
    source: str,
    filename: str = "",
    allowed: dict[str, frozenset[str]] | None = None,
) -> list[int]:
    """Return the lines that can open, create-over, delete or change a file.

    Everything that is not reading or `ALLOWED_CALLS` is flagged: see the
    module docstring for the rules. `filename` selects the allowlist entries.
    """
    allowed = ALLOWED_CALLS if allowed is None else allowed
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    exempt = _allowed_functions(tree, filename, allowed)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if _flagged_import(node):
                lines.add(node.lineno)
        elif isinstance(node, ast.Call):
            if node.func not in exempt and _is_flagged_string_lookup(node):
                lines.add(node.lineno)
        elif isinstance(node, ast.Name | ast.Attribute) and node not in exempt:
            qualified = _qualified(node, aliases)
            is_method = isinstance(node, ast.Attribute) and (
                node.attr in PATH_METHODS
                or (
                    node.attr in ARGUMENT_METHODS and _used_with_argument(node, parents)
                )
            )
            if qualified in FLAGGED_NAMES or is_method:
                lines.add(node.lineno)
            elif isinstance(node, ast.Name):
                parent = parents.get(node)
                is_base = isinstance(parent, ast.Attribute) and parent.value is node
                if node.id in DYNAMIC_NAMES or (
                    qualified in FILE_ACCESS_MODULES and not is_base
                ):
                    lines.add(node.lineno)
    return sorted(lines)


def test_sources_were_found():
    assert SOURCE_DIR.joinpath("errors.py") in SOURCES
    assert SOURCE_DIR.joinpath("adb.py") in SOURCES


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_network_libraries(path):
    found = banned_imports(path.read_text(encoding="utf-8"), FORBIDDEN_MODULES)
    assert not found, f"{path.name} imports forbidden modules: {found}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_socket_and_subprocess_only_in_adb(path):
    found = banned_imports(path.read_text(encoding="utf-8"), ADB_ONLY_MODULES)
    if path.name != ADB_MODULE:
        assert not found, f"{path.name} imports {found}; only adb.py may"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_dynamic_imports(path):
    lines = dynamic_imports(path.read_text(encoding="utf-8"))
    assert not lines, f"{path.name} imports by name at lines {lines}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_processes_are_started_only_by_subprocess_in_adb(path):
    lines = process_starters(path.read_text(encoding="utf-8"))
    assert not lines, f"{path.name} starts a process through os at lines {lines}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_sources_only_create_new_files(path):
    key = path.relative_to(SOURCE_DIR).as_posix()
    lines = file_changes(path.read_text(encoding="utf-8"), key)
    assert not lines, f"{path.name} can open or change files at lines {lines}"


def test_allowed_calls_are_used():
    """A stale allowlist entry would silently allow a new call of the same text."""
    for name, calls in ALLOWED_CALLS.items():
        tree = ast.parse(SOURCE_DIR.joinpath(name).read_text(encoding="utf-8"))
        present = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert calls <= present, f"{name}: unused allowlist entries {calls - present}"


@pytest.mark.parametrize(
    ("name", "call"),
    [(name, call) for name, calls in ALLOWED_CALLS.items() for call in sorted(calls)],
)
def test_each_allowlist_entry_is_needed(name, call):
    """Without this one entry (others kept) the file is flagged."""
    source = SOURCE_DIR.joinpath(name).read_text(encoding="utf-8")
    others = {name: ALLOWED_CALLS[name] - {call}}
    assert file_changes(source, name, others), f"{name}: {call!r} is not needed"


def test_every_import_of_the_sources_is_approved():
    """The list holds nothing spare.

    Missing ones fail test_sources_only_create_new_files.
    """
    imported: set[str] = set()
    for path in SOURCES:
        imported |= imported_modules(path.read_text(encoding="utf-8"))
    own = {name for name in imported if _is_own_module(name)}
    assert imported - own >= set(APPROVED_IMPORTS), (
        f"approved but not imported: {set(APPROVED_IMPORTS) - imported}"
    )


def test_approved_imports_have_reasons_and_no_writer_is_approved():
    for name, reason in APPROVED_IMPORTS.items():
        assert reason.strip(), name
        assert not is_under(name, frozenset(NEVER_APPROVE)), name
    for name, reason in NEVER_APPROVE.items():
        assert reason.strip(), name
        assert name not in APPROVED_IMPORTS


@pytest.mark.parametrize("module", sorted(APPROVED_IMPORTS))
def test_an_approved_import_is_not_flagged(module):
    assert not file_changes(f"import {module}", "other.py")
    if module != "__future__":  # `from __future__ import x` fails to compile
        assert not file_changes(f"from {module} import x_name", "other.py")


@pytest.mark.parametrize("module", sorted(NEVER_APPROVE))
def test_a_never_approved_import_is_flagged(module):
    assert file_changes(f"import {module}", "other.py")
    assert file_changes(f"from {module} import x_name", "other.py")


def test_relative_and_own_imports_are_not_flagged():
    source = "from . import sibling\nfrom .x import y\nfrom uiprobe.errors import E"
    assert not file_changes(source, "other.py")
    assert not file_changes("import uiprobe.model", "other.py")


def test_project_has_no_runtime_dependencies():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []


# --- the checks themselves ---


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os.path\nimport socket as s", {"os.path", "socket"}),
        (
            "from urllib.request import urlopen",
            {"urllib.request", "urllib.request.urlopen"},
        ),
        ("from . import sibling\nfrom .x import y", set()),
        ("def f():\n    import ssl", {"ssl"}),
        ("from logging import handlers", {"logging", "logging.handlers"}),
    ],
)
def test_imported_modules(source, expected):
    assert imported_modules(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request",
        "from urllib import request",
        "import _ssl",
        "import logging.handlers",
        "from logging import handlers",
        "from logging.handlers import SysLogHandler",
        "from multiprocessing.connection import Client",
        "import concurrent.futures.process",
        "from concurrent.futures import process",
        "import importlib.util",
        "import ctypes",
        "import pty",
    ],
)
def test_forbidden_imports_are_found(source):
    assert banned_imports(source, FORBIDDEN_MODULES)


@pytest.mark.parametrize(
    "source",
    [
        "import logging",
        "import logging.config",
        "from concurrent import futures",
        "import json\nimport xml.etree.ElementTree",
        "import socket",
    ],
)
def test_allowed_imports_are_not_flagged(source):
    assert not banned_imports(source, FORBIDDEN_MODULES)


@pytest.mark.parametrize(
    "source", ["import _socket", "from _socket import socket", "import socket.x"]
)
def test_adb_only_imports_are_found(source):
    assert banned_imports(source, ADB_ONLY_MODULES)


@pytest.mark.parametrize(
    "source",
    [
        "__import__('socket')",
        "import importlib\nimportlib.import_module('socket')",
        "import builtins\nbuiltins.__import__('socket')",
        "m = import_module\nm('x')\nimport_module('y')",
    ],
)
def test_dynamic_imports_are_found(source):
    assert dynamic_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.system('curl x')",
        "import os\nos.popen('x')",
        "import os\nos.execv('x', [])",
        "import os\nos.spawnl(0, 'x')",
        "import os\nos.posix_spawn('x', [], {})",
        "import os\nos.fork()",
        "import os\nos.startfile('x')",
        "from os import system",
        "from os import path, execvp",
    ],
)
def test_process_starters_are_found(source):
    assert process_starters(source)


@pytest.mark.parametrize(
    "source",
    ["import os\nos.devnull\nos.fspath('x')\nos.environ", "from os import path, sep"],
)
def test_ordinary_os_use_is_not_flagged(source):
    assert not process_starters(source)


@pytest.mark.parametrize(
    "source",
    [
        # direct calls
        "import os\nos.remove('x')",
        "import os\nos.unlink('x')",
        "import os\nos.rename('a', 'b')",
        "import os\nos.renames('a', 'b')",
        "import os\nos.replace('a', 'b')",
        "import os\nos.rmdir('x')",
        "import os\nos.removedirs('x')",
        "import os\nos.truncate('x', 0)",
        "import os\nos.ftruncate(3, 0)",
        "import os\nos.fdopen(3, 'w')",
        "import os\nos.link('a', 'b')",
        "import os\nos.symlink('a', 'b')",
        "import os\nos.mkfifo('x')",
        "import os\nos.mknod('x')",
        "import os\nos.chmod('x', 0)",
        "import os\nos.chown('x', 0, 0)",
        "import os\nos.utime('x')",
        "import os\nos.mkdir('x')",
        "open('x')",
        "open('x', 'x')",
        "open('x', 'w')",
        "open('x', mode='a')",
        "import io\nio.open('x')",
        "import io\nio.FileIO('x')",
        "import io\nio.open_code('x')",
        "import builtins\nbuiltins.open('x')",
        "import codecs\ncodecs.open('x', 'w')",
        "import os\nos.open('x', os.O_RDONLY)",
        "import os\nos.open('x', os.O_CREAT | os.O_EXCL | os.O_WRONLY)",
        # aliases of modules and names
        "import os as o\no.remove('x')",
        "import os.path\nos.remove('x')",
        "from os import remove\nremove('x')",
        "from os import remove as r",
        "from os import path, rename",
        "from os import open as op\nop('x', 0)",
        "from os import *",
        "from io import *",
        "import io as i\ni.open('x')",
        "x = os",
        "import os\nf = os\nf.remove('x')",
        "import os\nuse(os)",
        "import builtins\nb = builtins",
        # a reference is enough: no call needed
        "writer = open\nwriter('victim', 'w')",
        "import os\nwriter = os.open\nwriter('victim', os.O_WRONLY)",
        "from os import open as o\nwriter = o",
        "import os\ncopy(src, dst, opener=os.open)",
        "def wrapper(path, opener=None): ...\nwrapper('victim', opener=open)",
        "import os\nfunctions = [os.replace, os.rename]",
        # Path methods, on any receiver
        "from pathlib import Path\nPath('x').unlink()",
        "from pathlib import Path as P\nP('x').rename('y')",
        "import pathlib\npathlib.Path('x').replace('y')",
        "def f(p):\n    p.replace(target=p)",
        "def f(p):\n    p.rmdir()",
        "def f(p):\n    p.write_text('x')",
        "def f(p):\n    p.write_bytes(b'x')",
        "def f(p):\n    p.touch()",
        "def f(p):\n    p.mkdir()",
        "def f(p):\n    p.symlink_to('y')",
        "def f(p):\n    p.hardlink_to('y')",
        "def f(p):\n    p.chmod(0)",
        "def f(p):\n    p.open()",
        "def f(p):\n    p.open('rb')",
        "def f(p):\n    return p.unlink",
        "from pathlib import Path\nPath.replace(p, q)",
        "import pathlib\nf = pathlib.Path.replace",
        "def f(p):\n    p.move('y')",
        "def f(p):\n    p.move_into('y')",
        "import webbrowser\nwebbrowser.open",  # accepted false positive
        "'a-b'.replace('-', '_')",  # accepted false positive: allow-list it
        # modules that write or delete files
        "import shutil",
        "import shutil\nshutil.unpack_archive('a.zip', 'out')",
        "from shutil import copyfileobj",
        "import tempfile\ntempfile.mkdtemp()",
        "from tempfile import NamedTemporaryFile",
        "import tempfile as t",
        "import zipfile\nzipfile.ZipFile('a.zip').extractall('out')",
        "import tarfile",
        "import gzip",
        "import bz2\nbz2.BZ2File('x', 'w')",
        "import lzma",
        "import logging\nlogging.FileHandler('x', mode='w')",
        "import logging.config",
        "from logging import FileHandler",
        "import mmap",
        "import fileinput",
        "import dbm",
        "import dbm.dumb",
        "import shelve",
        "import sqlite3",
        "import posix",
        "import nt",
        "import _io",
        # ordinary writer modules (not on APPROVED_IMPORTS)
        'import cProfile\ncProfile.Profile().dump_stats("victim")',
        'import pstats\npstats.Stats().dump_stats("victim")',
        'from wave import open as wave_open\nwave_open("victim", "wb")',
        "import wave",
        "import cProfile",
        "import pstats",
        "import madeup_module_name",
        "from madeup_module_name import thing",
        "import madeup.sub.module",
        "import xml.dom.minidom",  # a submodule of nothing approved
        "import os.path as p\nimport csv",
        "from os import path\nimport pickle",
        "import pickle\npickle.dumps(1)",
        "import webbrowser",
        "def f():\n    import wave",
        "try:\n    import madeup\nexcept ImportError:\n    pass",
        "from xml.etree import ElementTree",  # xml.etree itself is not approved
        "from json import tool",  # a submodule of an approved package
        "from json import decoder",
        "from os import path as p",
        "from xml.etree.ElementTree import ElementTree\nimport wave",
        # streams and descriptors that truncate or write
        "def f(stream):\n    stream.seek(0)\n    stream.truncate(0)",
        "def f(stream):\n    return stream.truncate",
        "def f(t):\n    t.write_c14n('x')",
        "import os\nos.write(3, b'x')",
        "import os\nos.writev(3, [b'x'])",
        "import os\nos.pwrite(3, b'x', 0)",
        "import os\nos.pwritev(3, [b'x'], 0)",
        "import os\nos.eventfd_write(3, 1)",
        "import os as o\no.eventfd_write(3, 1)",
        "from os import eventfd_write\neventfd_write(3, 1)",
        "from os import eventfd_write as w",
        "import os\nos.sendfile(3, 4, 0, 1)",
        "import os\nos.splice(3, 4, 1)",
        "import os\nos.copy_file_range(3, 4, 1)",
        "from os import write",
        "from os import pwrite as p",
        "import os\nos.setxattr('x', 'a', b'v')",
        "import os\nos.chflags('x', 0)",
        # names given as strings
        "import os\ngetattr(os, 'replace')('a', 'b')",
        "getattr(x, 'open')",
        "getattr(x, 'unlink', None)",
        "getattr(x, name)",
        "setattr(x, name, 1)",
        "__import__('shutil')",
        "__import__('os').remove('x')",
        "import importlib\nimportlib.import_module('tempfile')",
        "import_module('os.path.x') or getattr(m, 'remove')",
        "eval('1')",
        "exec('1')",
        "globals()['open']",
        "__builtins__.x",
        # Python 3.14 copies, writers, argparse.FileType
        "def f(p, q):\n    p.copy(q)",
        "def f(p, q):\n    p.copy(target=q)",
        "def f(p, q):\n    p.copy_into(q)",
        "def f(p):\n    return p.copy",
        "import xml.etree.ElementTree as ET\nET.ElementTree(root).write('out.xml')",
        "def f(h):\n    h.writelines([])",
        "import argparse\nargparse.FileType('w')",
        "from argparse import FileType",
        "from argparse import *\nFileType('w')('victim')",
        "import argparse as ap\nap.FileType('w')",
        "import argparse\nf = argparse\nf.FileType('w')",
    ],
)
def test_file_changes_are_found(source):
    assert file_changes(source, "other.py")


# The one allowed call, with anything different: flagged in the very file that
# has the allowlist entry, not only in a file without one.
DEVNULL_VARIANTS = [
    "import os\nos.open(os.devnull, os.O_RDWR)",
    "import os\nos.open(os.devnull, os.O_WRONLY | os.O_TRUNC)",
    "import os\nos.open(os.devnull, os.O_WRONLY, 0o600)",
    "import os\nos.open(os.devnull, os.O_WRONLY, dir_fd=3)",
    "import os\nos.open('/dev/null', os.O_WRONLY)",
    "import os\nos.open(os.devnull + 'x', os.O_WRONLY)",
    "import os\nos.open(path=os.devnull, flags=os.O_WRONLY)",
    "import os as o\no.open(o.devnull, o.O_WRONLY)",
    "from os import open as op, devnull, O_WRONLY\nop(devnull, O_WRONLY)",
    "import os\nos.open(os.devnull, os.O_WRONLY)\nopen('x', 'w')",
    "import os\nos.open(os.open('x', 0), os.O_WRONLY)",
]


@pytest.mark.parametrize("source", DEVNULL_VARIANTS)
@pytest.mark.parametrize("filename", ["cli.py", "other.py"])
def test_a_changed_devnull_call_is_flagged_in_every_file(source, filename):
    assert file_changes(source, filename)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.open(os.devnull, os.O_WRONLY)",
        "import os\nos.open(os.devnull, os.O_WRONLY)  # discard output",
    ],
)
def test_the_allowed_call_is_accepted_only_in_its_file(source):
    assert not file_changes(source, "cli.py")
    assert file_changes(source, "device.py")
    assert file_changes(source, "sub/cli.py")  # the key is the whole relative path
    assert file_changes(source)


def test_an_allowed_call_still_has_its_arguments_checked():
    source = "import os\nos.open(os.devnull, os.O_WRONLY)\nopen('x', 'w')"
    assert file_changes(source, "cli.py") == [3]
    nested = "import os\nos.open(os.devnull, os.O_WRONLY or os.remove('x'))"
    assert file_changes(nested, "cli.py") == [2]


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.devnull\nos.fspath('x')\nos.environ",
        "import os\nos.path.join('a', 'b')\nos.close(3)\nos.dup2(3, 4)",
        "import os\nos.fstat(3)\nos.access('x', os.X_OK)\nos.pathsep",
        "from pathlib import Path\nPath('x').read_text()\nPath('x').exists()",
        "def f(p):\n    p.is_file()\n    p.stat()\n    p.resolve()",
        "import io\nisinstance(x, io.TextIOWrapper)",
        "import os\ndef f(p: os.PathLike[str]) -> None: ...",
        "import stat\nstat.S_ISFIFO(1)",
        "import json\njson.dumps({})\njson.loads('{}')",
        "from json import loads",
        "getattr(args, 'command_serial', None)",
        "import secrets\nfrom datetime import datetime\ndatetime.now()",
        "def opened(x): ...\nopened(1)\nself._open()",
        "import os\nos.path.join('a')",
        "d = {}\nd.copy()",
        "def f(d):\n    return d.copy()",
    ],
)
def test_ordinary_code_is_not_flagged(source):
    assert not file_changes(source, "other.py")
