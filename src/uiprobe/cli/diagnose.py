"""The diagnose command."""

from __future__ import annotations

import argparse  # noqa: TC003 (a runtime import in the former cli.py)
from dataclasses import replace
from typing import TYPE_CHECKING

from uiprobe._json import dumps
from uiprobe.cli.commands import _single_serial
from uiprobe.cli.output import _emit, _fail
from uiprobe.cli.parser import _UsageError
from uiprobe.crawl_driver import _app_named, _app_window, _takes_named
from uiprobe.crawl_preview import MAX_PACKAGE_LENGTH, check_package
from uiprobe.device import Device
from uiprobe.errors import INTERNAL_ERROR, USAGE_ERROR, UiprobeError
from uiprobe.windowdiag import APP_CRAWL_TESTED, APP_UID_UNREADABLE, TapWindowDiagnosis

if TYPE_CHECKING:
    from collections.abc import Callable

    from uiprobe.inputwindows import InputWindow
    from uiprobe.packageuid import PackageUid


def _diagnose_failure(
    kind: tuple[str, int], *, as_json: bool, details: dict[str, object] | None = None
) -> int:
    """Report a failed diagnosis by its error code alone: the error's own
    message can name the device (its serial, or text it printed)."""
    code, _ = kind
    message = (
        f"the check could not run ({code}); no more is shown, as the error"
        " can name the device"
    )
    return _fail(kind, message, as_json=as_json, details=details)


# The package of --package is the user's own input: it is not echoed, so
# the report's notice (no app names) stays true.
_DIAGNOSE_PACKAGE_MESSAGE = (
    "--package is not a valid package name (it is not shown): two or more"
    " segments separated by dots, each an ASCII letter followed by letters,"
    f" digits or underscores; at most {MAX_PACKAGE_LENGTH} characters"
)


def _diagnose_package(args: argparse.Namespace) -> str | None:
    """The package of --package checked as `crawl PACKAGE` checks its own,
    None without the option. The refusal does not echo it."""
    if args.package is None:
        return None
    package = str.__str__(args.package)
    try:
        return check_package(package)
    except ValueError:
        raise _UsageError(_DIAGNOSE_PACKAGE_MESSAGE) from None


def _crawl_owned(package: str, app: PackageUid) -> Callable[[InputWindow], bool]:
    """The crawl's own test of the app's windows (`_app_window`, which
    `_Run.app_window` calls), for `package` whose uid read as `app`."""

    def owned(window: InputWindow) -> bool:
        return _app_window(package, window, app)

    return owned


def _crawl_named(package: str) -> Callable[[InputWindow], bool]:
    """The crawl's name test of the app's windows (`_app_named`), for
    `package`."""

    def named(window: InputWindow) -> bool:
        return _app_named(package, window)

    return named


def _cmd_diagnose(args: argparse.Namespace) -> int:
    serial = _single_serial(args)
    package = _diagnose_package(args)
    try:
        try:
            device = Device(
                serial,
                adb_server=args.adb_server,
                adb=args.adb,
                start_server=args.start_server,
            )
        except (TypeError, ValueError):  # a malformed serial or server address
            return _fail(USAGE_ERROR, _DIAGNOSE_USAGE_MESSAGE, as_json=args.json)
        found = _diagnose_on(device, package, show_device_text=args.show_device_text)
    except UiprobeError as exc:
        return _diagnose_failure((exc.code, exc.exit_code), as_json=args.json)
    except Exception as exc:  # noqa: BLE001 - its message could hold device text
        details: dict[str, object] = {"type": type(exc).__name__}
        return _diagnose_failure(INTERNAL_ERROR, as_json=args.json, details=details)
    _emit(dumps(found.to_dict(), indent=2) if args.json else found.to_text())
    return 0


def _diagnose_on(
    device: Device, package: str | None, *, show_device_text: bool
) -> TapWindowDiagnosis:
    """Read the window list, with the app's own windows told as the crawl
    tells them when a package is given: its uid first (as the crawl reads it
    at its start), then ``dumpsys input``. A uid that cannot be read tells
    no window (the report's note says so)."""
    if package is None:
        return device.diagnose_tap_window(show_device_text=show_device_text)
    app = device.package_uid(package)
    if app is None:
        found = device.diagnose_tap_window(show_device_text=show_device_text)
        return replace(found, app_test=APP_UID_UNREADABLE)
    if _takes_named(device.diagnose_tap_window):
        found = device.diagnose_tap_window(
            show_device_text=show_device_text,
            owned=_crawl_owned(package, app),
            named=_crawl_named(package),
        )
    else:  # an override from before `named`: those values then refuse
        found = device.diagnose_tap_window(
            show_device_text=show_device_text, owned=_crawl_owned(package, app)
        )
    return replace(found, app_test=APP_CRAWL_TESTED)


# argparse quotes the words it cannot place, and with diagnose those can be a
# serial or other text that names the device.
_DIAGNOSE_USAGE_MESSAGE = (
    "the arguments of diagnose could not be read (they are not shown, as they"
    " can name the device); use: uiprobe [-s SERIAL] diagnose tap-window"
    " [-s SERIAL] [--adb PATH] [--adb-server tcp:HOST:PORT] [--no-start-server]"
    " [--json] [--show-device-text] [--package PKG]; device options go after"
    " tap-window or before diagnose"
)
