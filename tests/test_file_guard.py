"""Tests for the file-change guard in conftest.py (uiprobe only creates files).

The rules are described in the docstring of conftest.py. Each rule has a test
here that fails when the rule is taken out.
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import tests.conftest
from tests.conftest import FileModificationBlockedError

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _expect_blocked_attempts(blocked_attempts: list[str]) -> Iterator[None]:
    """These tests block on purpose; the guard fixture would fail them."""
    yield
    blocked_attempts.clear()


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A path next to tmp_path, so outside the test's root (and never created)."""
    return tmp_path.parent / f"{tmp_path.name}-outside"


def test_remove_outside_tmp_path_is_blocked(outside, blocked_attempts):
    with pytest.raises(FileModificationBlockedError):
        os.remove(outside / "file")  # noqa: PTH107
    assert len(blocked_attempts) == 1


def test_unlink_outside_tmp_path_is_blocked(outside):
    with pytest.raises(FileModificationBlockedError):
        (outside / "file").unlink()


def test_rename_to_outside_tmp_path_is_blocked(tmp_path, outside):
    source = tmp_path / "source"
    source.write_text("x", encoding="utf-8")
    with pytest.raises(FileModificationBlockedError):
        source.rename(outside / "target")
    assert source.exists()


def test_rename_from_outside_tmp_path_is_blocked(tmp_path, outside):
    with pytest.raises(FileModificationBlockedError):
        (outside / "source").rename(tmp_path / "target")


def test_rmtree_outside_tmp_path_is_blocked(outside):
    with pytest.raises(FileModificationBlockedError):
        shutil.rmtree(outside)


def test_rmdir_and_truncate_outside_tmp_path_are_blocked(outside):
    with pytest.raises(FileModificationBlockedError):
        os.rmdir(outside)  # noqa: PTH106
    with pytest.raises(FileModificationBlockedError):
        os.truncate(outside / "file", 0)


def test_a_relative_path_is_judged_where_it_points(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    with pytest.raises(FileModificationBlockedError):
        os.remove("not-in-tmp-path")  # noqa: PTH107


def test_a_relative_truncate_is_judged_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    with pytest.raises(FileModificationBlockedError):
        os.truncate("not-in-tmp-path", 0)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_a_truncate_length_is_never_taken_for_a_dir_fd(tmp_path, monkeypatch):
    # The length of os.truncate is its second argument. Here it equals a valid
    # descriptor of a directory under tmp_path; the name itself is relative to
    # the current directory, which is outside tmp_path.
    monkeypatch.chdir(tmp_path.parent)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(FileModificationBlockedError):
            os.truncate("not-in-tmp-path", fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_a_name_relative_to_an_outside_dir_fd_is_blocked(tmp_path):
    fd = os.open(tmp_path.parent, os.O_RDONLY)  # exists, outside tmp_path
    try:
        with pytest.raises(FileModificationBlockedError):
            os.remove("not-in-tmp-path", dir_fd=fd)
    finally:
        os.close(fd)


def test_changes_inside_tmp_path_are_allowed(tmp_path, blocked_attempts):
    first = tmp_path / "first"
    first.write_text("abc", encoding="utf-8")
    os.truncate(first, 1)
    assert first.read_text(encoding="utf-8") == "a"
    second = tmp_path / "second"
    first.rename(second)
    second.replace(first)
    first.unlink()
    folder = tmp_path / "folder"
    (folder / "deep").mkdir(parents=True)
    (folder / "deep" / "file").write_text("x", encoding="utf-8")
    shutil.rmtree(folder)
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty").rmdir()
    assert not blocked_attempts
    assert list(tmp_path.iterdir()) == []


PROJECT = Path(__file__).resolve().parent.parent
needs_proc = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd"
)
needs_dir_fd = pytest.mark.skipif(
    os.unlink not in os.supports_dir_fd or os.rename not in os.supports_dir_fd,
    reason="needs dir_fd support",
)
needs_fd_truncate = pytest.mark.skipif(
    os.truncate not in os.supports_fd, reason="needs os.truncate on descriptors"
)
# os.open() on a directory raises PermissionError on Windows.
not_on_windows = pytest.mark.skipif(
    sys.platform == "win32", reason="a directory cannot be opened as a descriptor"
)


@pytest.fixture
def directory_descriptors() -> Iterator[list[int]]:
    """Collect descriptors of directories; close them after the test."""
    descriptors: list[int] = []
    yield descriptors
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            continue


def open_directory(path: Path, descriptors: list[int]) -> int:
    fd = os.open(path, os.O_RDONLY)
    descriptors.append(fd)
    return fd


def closed_descriptor() -> int:
    """Return a descriptor number that is not open (and stays so)."""
    if hasattr(os, "sysconf") and "SC_OPEN_MAX" in os.sysconf_names:
        # Above the limit of open descriptors, so no thread can get it.
        return max(os.sysconf("SC_OPEN_MAX"), 1024) + 1
    fd = os.open(os.devnull, os.O_RDONLY)
    os.close(fd)
    return fd


# (f) housekeeping directories only inside the repository


def test_pycache_is_allowed_inside_the_repository():
    # The files do not exist: reaching the OS error means the guard let it by.
    for place in (
        PROJECT / "tests" / "__pycache__" / "no-such-file",
        PROJECT / "src" / "uiprobe" / "__pycache__" / "no-such-file",
        PROJECT / ".pytest_cache" / "no-such-file",
        PROJECT / ".pytest_cache" / "v" / "cache" / "no-such-file",
    ):
        with pytest.raises(FileNotFoundError):
            os.remove(place)  # noqa: PTH107


def test_pycache_and_pytest_cache_are_blocked_outside_the_repository(outside):
    for name in ("__pycache__", ".pytest_cache"):
        with pytest.raises(FileModificationBlockedError):
            os.remove(outside / name / "file")  # noqa: PTH107
        with pytest.raises(FileModificationBlockedError):
            os.remove(Path(tempfile.gettempdir()) / name / "file")  # noqa: PTH107


def test_only_exact_housekeeping_names_inside_the_repository_are_allowed():
    for place in (
        PROJECT / "README.md",
        PROJECT / "src" / "uiprobe" / "adb.py",
        PROJECT / "not__pycache__" / "file",
        PROJECT / "__pycache__x" / "file",
        PROJECT / ".pytest_cache2" / "file",
    ):
        with pytest.raises(FileModificationBlockedError):
            os.remove(place)  # noqa: PTH107


# (e) tempfile's own names directly inside the temporary directory


def temporary_directory() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def test_unlinking_a_tempfile_name_in_the_temporary_directory_is_allowed():
    with pytest.raises(FileNotFoundError):
        os.remove(temporary_directory() / "tmpzq8_x3k1")  # noqa: PTH107


@pytest.mark.parametrize(
    "name",
    [
        "tmpzq8_x3k",  # too short
        "tmpzq8_x3k1a",  # too long
        "tmpZQ8_X3K1",  # not tempfile's alphabet
        "tmpzq8-x3k1",
        "Tmpzq8_x3k1",
        "tmpzq8_x3k1.txt",  # TemporaryFile has no suffix
        "other-file",
        "tmp",
        "pytest-of-user",
    ],
)
def test_other_names_in_the_temporary_directory_are_blocked(name):
    with pytest.raises(FileModificationBlockedError):
        os.remove(temporary_directory() / name)  # noqa: PTH107


def test_a_tempfile_name_is_allowed_only_directly_inside_the_directory():
    name = "tmpzq8_x3k1"
    with pytest.raises(FileModificationBlockedError):
        os.remove(temporary_directory() / "sub" / name)  # noqa: PTH107
    with pytest.raises(FileModificationBlockedError):
        os.remove(temporary_directory().parent / name)  # noqa: PTH107


def test_only_unlinking_is_allowed_for_a_tempfile_name():
    path = temporary_directory() / "tmpzq8_x3k1"
    with pytest.raises(FileModificationBlockedError):
        os.rmdir(path)  # noqa: PTH106
    with pytest.raises(FileModificationBlockedError):
        os.truncate(path, 0)
    with pytest.raises(FileModificationBlockedError):
        shutil.rmtree(path)
    with pytest.raises(FileModificationBlockedError):
        os.rename(path, path.with_name("tmpzq8_x3k2"))  # noqa: PTH104


def test_the_name_pattern_is_what_tempfile_makes(blocked_attempts):
    for _ in range(20):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        try:
            assert Path(name).resolve().parent == temporary_directory()
            assert tests.conftest._TEMP_FILE_NAME.fullmatch(Path(name).name)
        finally:
            os.remove(name)  # noqa: PTH107
    assert not blocked_attempts


def test_a_temporary_file_without_o_tmpfile_is_not_blocked(
    monkeypatch, blocked_attempts
):
    # tempfile then makes a named file and unlinks it at once.
    monkeypatch.setattr(tempfile, "_O_TMPFILE_WORKS", False, raising=False)
    with tempfile.TemporaryFile() as handle:
        handle.write(b"x")
    assert not blocked_attempts


@pytest.fixture
def without_o_tmpfile(monkeypatch):
    monkeypatch.setattr(tempfile, "_O_TMPFILE_WORKS", False, raising=False)


@pytest.mark.usefixtures("without_o_tmpfile")
def test_capfd_works_without_o_tmpfile(capfd, blocked_attempts):
    # capfd creates its temporary files in its own setup, inside the guard.
    print("hello")  # noqa: T201
    assert capfd.readouterr().out == "hello\n"
    assert not blocked_attempts


# (a) a rename resolves each name against its own descriptor


@needs_proc
@needs_dir_fd
def test_a_rename_into_a_directory_outside_by_dir_fd_is_blocked(
    tmp_path, monkeypatch, directory_descriptors
):
    # The current directory is inside, so judging against it would allow it.
    monkeypatch.chdir(tmp_path)
    inside = open_directory(tmp_path, directory_descriptors)
    outside_fd = open_directory(tmp_path.parent, directory_descriptors)
    with pytest.raises(FileModificationBlockedError):
        os.rename("a", "b", src_dir_fd=inside, dst_dir_fd=outside_fd)


@needs_proc
@needs_dir_fd
def test_a_rename_from_a_directory_outside_by_dir_fd_is_blocked(
    tmp_path, monkeypatch, directory_descriptors
):
    monkeypatch.chdir(tmp_path)
    inside = open_directory(tmp_path, directory_descriptors)
    outside_fd = open_directory(tmp_path.parent, directory_descriptors)
    with pytest.raises(FileModificationBlockedError):
        os.rename("a", "b", src_dir_fd=outside_fd, dst_dir_fd=inside)


@needs_proc
@needs_dir_fd
def test_a_rename_by_dir_fd_inside_is_allowed_from_any_current_directory(
    tmp_path, monkeypatch, blocked_attempts, directory_descriptors
):
    (tmp_path / "a").write_text("x", encoding="utf-8")
    inside = open_directory(tmp_path, directory_descriptors)
    monkeypatch.chdir(tmp_path.parent)  # judging against it would block
    os.rename("a", "b", src_dir_fd=inside, dst_dir_fd=inside)
    os.replace("b", "c", src_dir_fd=inside, dst_dir_fd=inside)
    assert (tmp_path / "c").read_text(encoding="utf-8") == "x"
    assert not blocked_attempts


@needs_proc
@needs_dir_fd
def test_a_rename_refuses_a_destination_descriptor_it_cannot_resolve(
    tmp_path, directory_descriptors
):
    inside = open_directory(tmp_path, directory_descriptors)
    with pytest.raises(FileModificationBlockedError):
        os.rename("a", "b", src_dir_fd=inside, dst_dir_fd=closed_descriptor())
    with pytest.raises(FileModificationBlockedError):
        os.rename("a", "b", src_dir_fd=closed_descriptor(), dst_dir_fd=inside)


# (b) a descriptor-relative operation with an unresolvable descriptor is refused


@needs_dir_fd
def test_a_dir_fd_that_cannot_be_resolved_is_refused(
    tmp_path, monkeypatch, directory_descriptors
):
    inside = open_directory(tmp_path, directory_descriptors)
    # Were the name judged against the current directory it would be allowed.
    monkeypatch.chdir(tmp_path)
    # The platform cannot say where a descriptor points (no /proc/self/fd).
    monkeypatch.setattr(tests.conftest, "_path_of_descriptor", lambda _fd: None)
    with pytest.raises(FileModificationBlockedError):
        os.remove("file", dir_fd=inside)
    with pytest.raises(FileModificationBlockedError):
        os.rmdir("folder", dir_fd=inside)
    with pytest.raises(FileModificationBlockedError):
        shutil.rmtree("folder", dir_fd=inside)


@needs_dir_fd
def test_a_closed_dir_fd_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileModificationBlockedError):
        os.remove("file", dir_fd=closed_descriptor())


@needs_proc
@needs_dir_fd
def test_a_dir_fd_that_is_not_a_directory_path_is_refused():
    read_end, write_end = os.pipe()  # /proc says "pipe:[...]", not a path
    try:
        with pytest.raises(FileModificationBlockedError):
            os.remove("file", dir_fd=read_end)
    finally:
        os.close(read_end)
        os.close(write_end)


@needs_dir_fd
def test_an_absolute_name_needs_no_descriptor(tmp_path, blocked_attempts):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    os.remove(tmp_path / "file", dir_fd=closed_descriptor())
    assert not blocked_attempts


# (c) os.truncate with an integer descriptor


@needs_proc
@needs_fd_truncate
def test_truncating_a_descriptor_of_a_file_inside_is_allowed(
    tmp_path, blocked_attempts
):
    path = tmp_path / "file"
    path.write_text("abc", encoding="utf-8")
    fd = os.open(path, os.O_RDWR)
    try:
        os.truncate(fd, 2)
        os.ftruncate(fd, 1)
    finally:
        os.close(fd)
    assert path.read_text(encoding="utf-8") == "a"
    assert not blocked_attempts


@needs_fd_truncate
@not_on_windows
def test_truncating_a_descriptor_that_points_outside_is_blocked(
    tmp_path, directory_descriptors
):
    # A directory cannot be truncated: reaching the OS error means it was let by.
    fd = open_directory(tmp_path.parent, directory_descriptors)
    with pytest.raises(FileModificationBlockedError):
        os.truncate(fd, 0)
    with pytest.raises(FileModificationBlockedError):
        os.ftruncate(fd, 0)


@needs_fd_truncate
def test_truncating_a_descriptor_that_cannot_be_resolved_is_refused(
    tmp_path, monkeypatch
):
    with pytest.raises(FileModificationBlockedError):
        os.truncate(closed_descriptor(), 0)
    # A regular file inside tmp_path, so only the failed lookup can refuse it.
    path = tmp_path / "file"
    path.write_text("abc", encoding="utf-8")
    fd = os.open(path, os.O_RDWR)
    try:
        monkeypatch.setattr(tests.conftest, "_path_of_descriptor", lambda _fd: None)
        with pytest.raises(FileModificationBlockedError):
            os.truncate(fd, 0)
    finally:
        os.close(fd)
    assert path.read_text(encoding="utf-8") == "abc"


# (d) os.truncate follows a symlink, os.remove and os.rename do not


def test_truncating_a_symlink_to_outside_is_blocked(link_to_outside):
    with pytest.raises(FileModificationBlockedError):
        os.truncate(link_to_outside, 0)


def test_truncating_a_symlink_to_a_file_inside_is_allowed(tmp_path, blocked_attempts):
    target = tmp_path / "target"
    target.write_text("abc", encoding="utf-8")
    link = tmp_path / "link-to-file"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    os.truncate(link, 1)
    assert target.read_text(encoding="utf-8") == "a"
    assert not blocked_attempts


def test_renaming_a_symlink_to_outside_moves_the_link_itself(
    tmp_path, link_to_outside, blocked_attempts
):
    link_to_outside.rename(tmp_path / "moved")
    assert (tmp_path / "moved").is_symlink()
    assert not blocked_attempts


def test_bytes_paths_are_judged_like_str_paths(tmp_path, outside, blocked_attempts):
    folder = tmp_path / "folder"
    (folder / "deep").mkdir(parents=True)
    (folder / "deep" / "file").write_text("x", encoding="utf-8")
    shutil.rmtree(os.fsencode(folder))
    assert not folder.exists()
    assert not blocked_attempts
    with pytest.raises(FileModificationBlockedError):
        shutil.rmtree(os.fsencode(outside))
    with pytest.raises(FileModificationBlockedError):
        os.remove(os.fsencode(outside / "file"))  # noqa: PTH107


@pytest.fixture
def link_to_outside(tmp_path: Path, outside: Path) -> Path:
    """A symlink inside tmp_path that points at `outside` (which does not exist)."""
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    return link


def test_removing_a_symlink_is_judged_by_the_link_not_its_target(
    link_to_outside, blocked_attempts
):
    link_to_outside.unlink()
    assert not link_to_outside.is_symlink()
    assert not blocked_attempts


def test_a_name_below_a_symlink_to_outside_is_blocked(link_to_outside):
    with pytest.raises(FileModificationBlockedError):
        os.remove(link_to_outside / "file")  # noqa: PTH107


# an import during a test may write a .pyc; that must neither fail nor leave one


def test_importing_a_module_outside_the_repository_writes_no_bytecode(
    tmp_path, blocked_attempts, monkeypatch
):
    folder = tmp_path.parent / f"{tmp_path.name}-modules"
    folder.mkdir()
    (folder / "fresh_guard_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    try:
        module = importlib.import_module("fresh_guard_module")
        assert module.VALUE == 1
        assert not blocked_attempts
        assert not (folder / "__pycache__").exists()
    finally:
        sys.modules.pop("fresh_guard_module", None)
        # The guard (rightly) refuses to delete outside tmp_path: disarm it
        # for the clean-up of this test's own folder.
        root = tests.conftest._STATE.tmp_root
        tests.conftest._STATE.tmp_root = None
        try:
            shutil.rmtree(folder)
        finally:
            tests.conftest._STATE.tmp_root = root


# the resolver of descriptors: /proc/self/fd on Linux, fcntl.F_GETPATH on macOS


def _can_resolve_descriptors() -> bool:
    if Path("/proc/self/fd").is_dir():
        return True
    fcntl = tests.conftest._load_fcntl()
    return fcntl is not None and hasattr(fcntl, "F_GETPATH")


@pytest.mark.skipif(
    not _can_resolve_descriptors(), reason="needs /proc/self/fd or fcntl.F_GETPATH"
)
def test_a_descriptor_resolves_to_its_path(tmp_path, directory_descriptors):
    path = tmp_path / "file"
    path.write_text("x", encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    directory_descriptors.append(fd)
    assert tests.conftest._path_of_descriptor(fd) == path.resolve()
    if sys.platform != "win32":
        folder = open_directory(tmp_path, directory_descriptors)
        assert tests.conftest._path_of_descriptor(folder) == tmp_path.resolve()
    assert tests.conftest._path_of_descriptor(closed_descriptor()) is None


class _FakeFcntl:
    """Stands in for the macOS fcntl: F_GETPATH fills a NUL-padded buffer."""

    F_GETPATH = 50

    def __init__(self, answer: bytes | OSError) -> None:
        self.answer = answer
        self.requests: list[tuple[int, int]] = []

    def fcntl(self, fd: int, request: int, buffer: bytes) -> bytes:
        assert len(buffer) >= 1024
        self.requests.append((fd, request))
        if isinstance(self.answer, OSError):
            raise self.answer
        return self.answer.ljust(len(buffer), b"\0")


def test_getpath_answers_are_taken_as_paths(tmp_path):
    fake = _FakeFcntl(os.fsencode(tmp_path))
    assert tests.conftest._path_by_getpath(7, fake) == tmp_path
    assert fake.requests == [(7, _FakeFcntl.F_GETPATH)]


def test_getpath_failures_give_no_path():
    assert tests.conftest._path_by_getpath(7, _FakeFcntl(OSError("bad fd"))) is None
    assert tests.conftest._path_by_getpath(7, _FakeFcntl(b"relative/path")) is None
    assert tests.conftest._path_by_getpath(7, _FakeFcntl(b"")) is None
    assert tests.conftest._path_by_getpath(7, object()) is None  # no F_GETPATH
    assert tests.conftest._path_by_getpath(7, None) is None  # no fcntl (Windows)


@pytest.mark.skipif(
    Path("/proc/self/fd").is_dir(), reason="the fallback is for hosts without /proc"
)
def test_a_descriptor_falls_back_to_getpath_without_proc(monkeypatch, tmp_path):
    fake = _FakeFcntl(os.fsencode(tmp_path))
    monkeypatch.setattr(tests.conftest, "_load_fcntl", lambda: fake)
    assert tests.conftest._path_of_descriptor(3) == tmp_path


def test_the_fallback_to_getpath_is_used_when_proc_cannot_answer(monkeypatch, tmp_path):
    # Runs everywhere: a descriptor /proc cannot answer for (closed) goes to fcntl.
    fake = _FakeFcntl(os.fsencode(tmp_path))
    monkeypatch.setattr(tests.conftest, "_load_fcntl", lambda: fake)
    fd = closed_descriptor()
    if Path(f"/proc/self/fd/{fd}").exists():
        pytest.skip("descriptor unexpectedly open")
    assert tests.conftest._path_of_descriptor(fd) == tmp_path
    assert fake.requests == [(fd, _FakeFcntl.F_GETPATH)]


@needs_dir_fd
def test_rmtree_inside_tmp_path_works_through_a_getpath_resolver(
    tmp_path, monkeypatch, blocked_attempts
):
    # macOS has no /proc: rmtree (dir_fd based there) must still be let through.
    real = tests.conftest._path_of_descriptor

    def by_getpath(fd: int) -> Path | None:
        path = real(fd)
        fake = _FakeFcntl(b"" if path is None else os.fsencode(path))
        return tests.conftest._path_by_getpath(fd, fake)

    monkeypatch.setattr(tests.conftest, "_path_of_descriptor", by_getpath)
    folder = tmp_path / "folder"
    (folder / "deep").mkdir(parents=True)
    (folder / "deep" / "file").write_text("x", encoding="utf-8")
    shutil.rmtree(folder)
    assert not folder.exists()
    assert not blocked_attempts


# write-mode opens inside the repository fail; elsewhere they are not judged
# Every victim sits in a folder that does not exist: a regressed guard then
# raises FileNotFoundError (still a failure) and leaves nothing behind.
VICTIM = PROJECT / "guard-missing-folder" / "file"


def test_a_write_mode_open_inside_the_repository_is_blocked(blocked_attempts):
    victim = VICTIM
    for mode in ("w", "a", "x", "wb", "r+", "w+", "ab", "xb", "rb+"):
        with pytest.raises(FileModificationBlockedError):
            open(victim, mode)  # noqa: PTH123, SIM115
    assert not victim.exists()
    assert not victim.parent.exists()
    assert len(blocked_attempts) == 9


def test_path_and_io_writers_inside_the_repository_are_blocked():
    victim = VICTIM
    with pytest.raises(FileModificationBlockedError):
        victim.write_text("x", encoding="utf-8")
    with pytest.raises(FileModificationBlockedError):
        victim.write_bytes(b"x")
    with pytest.raises(FileModificationBlockedError):
        victim.open("w")
    with pytest.raises(FileModificationBlockedError):
        io.open(victim, "w")  # noqa: SIM115, UP020
    with pytest.raises(FileModificationBlockedError):
        io.FileIO(victim, "w")
    assert not victim.exists()
    assert not victim.parent.exists()


@pytest.mark.parametrize(
    "flag", ["O_WRONLY", "O_RDWR", "O_TRUNC", "O_APPEND", "O_CREAT"]
)
def test_os_open_with_a_writing_flag_inside_the_repository_is_blocked(flag):
    victim = VICTIM
    with pytest.raises(FileModificationBlockedError):
        os.open(victim, getattr(os, flag))
    with pytest.raises(FileModificationBlockedError):
        os.open(victim, os.O_RDONLY | getattr(os, flag))
    assert not victim.exists()
    assert not victim.parent.exists()


def test_a_relative_write_inside_the_repository_is_blocked(monkeypatch):
    monkeypatch.chdir(PROJECT)
    with pytest.raises(FileModificationBlockedError):
        open("guard-missing-folder/file", "w")  # noqa: PTH123, SIM115
    monkeypatch.chdir(PROJECT / "tests")
    with pytest.raises(FileModificationBlockedError):
        open("guard-missing-folder/file", "w")  # noqa: PTH123, SIM115
    with pytest.raises(FileModificationBlockedError):
        open("../guard-missing-folder/file", "w")  # noqa: PTH123, SIM115
    assert not VICTIM.exists()
    assert not VICTIM.parent.exists()


def test_a_symlink_into_the_repository_is_judged_by_its_target(tmp_path):
    link = tmp_path / "link"
    try:
        link.symlink_to(VICTIM)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(FileModificationBlockedError):
        link.write_text("x", encoding="utf-8")
    assert not VICTIM.exists()
    assert not VICTIM.parent.exists()


def test_reading_inside_the_repository_is_allowed(blocked_attempts):
    assert (PROJECT / "pyproject.toml").read_bytes()
    with open(PROJECT / "pyproject.toml", encoding="utf-8") as handle:  # noqa: PTH123
        assert handle.read()
    descriptor = os.open(PROJECT / "pyproject.toml", os.O_RDONLY)
    os.close(descriptor)
    assert not blocked_attempts


def test_writing_inside_tmp_path_is_allowed(tmp_path, blocked_attempts):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    os.close(os.open(tmp_path / "other", os.O_WRONLY | os.O_CREAT, 0o600))
    assert not blocked_attempts


def test_writing_outside_the_repository_is_not_judged(outside, blocked_attempts):
    # The folder does not exist: reaching the OS error means the guard let it by.
    with pytest.raises(FileNotFoundError):
        open(outside / "missing-folder" / "file", "w")  # noqa: PTH123, SIM115
    with pytest.raises(FileNotFoundError):
        os.open(outside / "missing-folder" / "file", os.O_WRONLY | os.O_CREAT)
    assert not blocked_attempts


@pytest.mark.parametrize(
    "name",
    [
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".hypothesis",
        ".coverage",
        ".coverage.host.123.abc",
    ],
)
def test_tool_caches_inside_the_repository_may_be_written(name, blocked_attempts):
    # The cache folder is missing (or is a file): the OS error shows the guard
    # let the open by.
    place = PROJECT / "tests" / name / "guard-missing-folder" / "file"
    with pytest.raises(OSError):  # noqa: PT011
        open(place, "w")  # noqa: PTH123, SIM115
    with pytest.raises(OSError):  # noqa: PT011
        os.open(place, os.O_WRONLY | os.O_CREAT)
    assert not blocked_attempts


def test_a_name_that_only_looks_like_a_cache_is_blocked():
    for name in ("not__pycache__", ".pytest_cache2", ".ruff_cache_x", "coverage"):
        with pytest.raises(FileModificationBlockedError):
            open(PROJECT / name / "file", "w")  # noqa: PTH123, SIM115
        assert not (PROJECT / name).exists()


def test_a_descriptor_given_to_open_is_not_a_path(tmp_path, blocked_attempts):
    fd = os.open(tmp_path / "file", os.O_WRONLY | os.O_CREAT, 0o600)
    with open(fd, "w", encoding="utf-8") as handle:  # noqa: PTH123
        handle.write("x")
    assert not blocked_attempts


def test_a_write_open_is_judged_where_the_event_says():
    # Calls the check directly, with the arguments of the audit event.
    check = tests.conftest._check_open_event
    root = Path(tempfile.gettempdir())
    victim = str(VICTIM)
    with pytest.raises(FileModificationBlockedError):
        check((victim, "w", 0), root)
    with pytest.raises(FileModificationBlockedError):
        check((victim, None, os.O_WRONLY), root)
    check((victim, "r", 0), root)  # a read
    check((victim, None, os.O_RDONLY), root)
    check((str(root / "elsewhere"), "w", 0), root)  # outside the repository
    check((7, "w", 0), root)  # a descriptor
    check((victim, "w"), root)  # not the event's shape
    # a temporary root inside the repository (--basetemp) is exempt
    inside = PROJECT / "build-tmp"
    check((str(inside / "f"), "w", 0), inside)
