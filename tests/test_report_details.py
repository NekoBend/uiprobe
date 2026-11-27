"""The failure report names a uiprobe error's details (tests/conftest.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import conftest
from uiprobe.errors import AdbServerUnavailableError


def _report(error: BaseException | None, *, failed: bool) -> SimpleNamespace:
    excinfo = None
    if error is not None:
        try:
            raise error
        except BaseException:  # noqa: BLE001 - captured for the hook
            excinfo = pytest.ExceptionInfo.from_current()
    report = SimpleNamespace(failed=failed, sections=[])
    hook = conftest.pytest_runtest_makereport(
        call=SimpleNamespace(excinfo=excinfo),  # ty: ignore[invalid-argument-type]
    )
    next(hook)
    with pytest.raises(StopIteration) as stop:
        hook.send(report)  # ty: ignore[invalid-argument-type]
    assert stop.value.value is report
    return report


def test_a_failed_test_shows_the_details_of_a_uiprobe_error() -> None:
    error = AdbServerUnavailableError(
        "adb server is unavailable",
        details={"reason": "cannot connect: [Errno 111] refused"},
    )
    report = _report(error, failed=True)
    assert len(report.sections) == 1
    title, text = report.sections[0]
    assert title == "uiprobe error details"
    assert "Errno 111" in text


def test_a_uiprobe_error_behind_another_error_is_named_too() -> None:
    try:
        try:
            raise AdbServerUnavailableError("down", details={"reason": "timed out"})
        except AdbServerUnavailableError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        report = _report(outer, failed=True)
    assert "timed out" in report.sections[0][1]


def test_only_the_address_and_the_reason_are_shown() -> None:
    error = AdbServerUnavailableError(
        "down", details={"reason": "refused", "adb_path": "/home/x/adb"}
    )
    text = _report(error, failed=True).sections[0][1]
    assert "refused" in text
    assert "adb_path" not in text


def test_a_suppressed_context_is_not_shown() -> None:
    try:
        try:
            raise AdbServerUnavailableError("down", details={"reason": "hidden"})
        except AdbServerUnavailableError:
            raise RuntimeError("plain") from None
    except RuntimeError as outer:
        report = _report(outer, failed=True)
    assert report.sections == []


def test_a_detail_that_fails_to_print_keeps_the_report() -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("no")

    error = AdbServerUnavailableError("down", details={"reason": Broken()})
    assert _report(error, failed=True).sections == []


def test_a_passing_test_or_another_error_adds_nothing() -> None:
    assert _report(None, failed=False).sections == []
    assert _report(ValueError("x"), failed=True).sections == []
    error = AdbServerUnavailableError("down")  # no details
    assert _report(error, failed=True).sections == []
