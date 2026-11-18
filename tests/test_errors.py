"""Tests for uiprobe.errors."""

import json
from typing import TYPE_CHECKING, cast

import pytest

from uiprobe import errors
from uiprobe.errors import (
    INTERNAL_ERROR,
    INTERRUPTED,
    USAGE_ERROR,
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    DumpError,
    ElementNotFoundError,
    ElementNotVisibleError,
    InputFailedError,
    MultipleDevicesError,
    MultipleElementsError,
    ScreenshotFailedError,
    UiprobeError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

TABLE = [
    (UiprobeError, "internal_error", 1),
    (AdbServerUnavailableError, "adb_server_unavailable", 3),
    (DeviceNotFoundError, "device_not_found", 4),
    (MultipleDevicesError, "multiple_devices", 4),
    (DeviceOfflineError, "device_offline", 5),
    (DeviceUnauthorizedError, "device_unauthorized", 5),
    (AdbError, "adb_error", 10),
    (DumpError, "dump_failed", 11),
    (ElementNotFoundError, "element_not_found", 12),
    (MultipleElementsError, "multiple_elements", 13),
    (ElementNotVisibleError, "element_not_visible", 12),
    (InputFailedError, "input_failed", 14),
    (ScreenshotFailedError, "screenshot_failed", 16),
]


@pytest.mark.parametrize(("cls", "code", "exit_code"), TABLE)
def test_code_and_exit_code(cls, code, exit_code):
    assert cls.code == code
    assert cls.exit_code == exit_code
    assert issubclass(cls, UiprobeError)
    assert issubclass(cls, Exception)


def test_codes_are_unique_per_class():
    codes = [code for _, code, _ in TABLE]
    assert len(codes) == len(set(codes))


def test_every_error_class_is_in_the_table():
    classes = {
        obj
        for obj in vars(errors).values()
        if isinstance(obj, type) and issubclass(obj, UiprobeError)
    }
    assert classes == {row[0] for row in TABLE}


def test_message_and_str():
    err = DumpError("bad xml")
    assert err.message == "bad xml"
    assert str(err) == "bad xml"
    assert err.details == {}


def test_details_are_copied():
    source = {"a": 1}
    err = AdbError("boom", details=source)
    source["b"] = 2
    assert err.details == {"a": 1}


def test_to_dict_omits_empty_details():
    assert DeviceNotFoundError("none").to_dict() == {
        "schema": "uiprobe.error/1",
        "error": {"code": "device_not_found", "message": "none"},
    }


def test_to_dict_includes_details_and_is_json_serializable():
    err = MultipleDevicesError(
        "2 devices", details={"serials": ["emulator-5554", "emulator-5556"]}
    )
    data = err.to_dict()
    assert data == {
        "schema": "uiprobe.error/1",
        "error": {
            "code": "multiple_devices",
            "message": "2 devices",
            "details": {"serials": ["emulator-5554", "emulator-5556"]},
        },
    }
    assert json.loads(json.dumps(data)) == data


def test_empty_mapping_details_are_omitted():
    error = AdbError("x", details={}).to_dict()["error"]
    assert isinstance(error, dict)
    assert "details" not in error


def test_can_be_caught_as_base():
    with pytest.raises(UiprobeError) as info:
        raise ElementNotFoundError("missing")
    assert info.value.code == "element_not_found"


def test_message_is_required_and_details_keyword_only():
    cls = cast("Callable[..., UiprobeError]", UiprobeError)
    with pytest.raises(TypeError):
        cls()
    with pytest.raises(TypeError):
        cls("m", {"a": 1})


def test_cli_level_constants():
    assert USAGE_ERROR == ("usage_error", 2)
    assert INTERRUPTED == ("interrupted", 130)
    assert INTERNAL_ERROR == ("internal_error", 1)
