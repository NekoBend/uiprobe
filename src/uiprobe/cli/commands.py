"""The simple commands: devices, dump, find, tap, swipe, key, type, launch, init."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from uiprobe._json import dumps
from uiprobe.cli.crawl_dry import _check_config_path, _is_missing, _load_settings
from uiprobe.cli.output import _emit, _printable
from uiprobe.cli.parser import _MAX_MS, _UsageError
from uiprobe.crawl_preview import check_package
from uiprobe.device import (
    DEFAULT_LAUNCH_TIMEOUT,
    DEFAULT_PRESS_MS,
    DEFAULT_SWIPE_MS,
    MAX_LAUNCH_TIMEOUT,
    MAX_PRESS_MS,
    Device,
    DeviceDetails,
    DisplaySizeUnknownError,
    RefusedKeys,
    _key_code,
    _text_pieces,
    device_details,
    devices,
)
from uiprobe.model import MAX_CANDIDATES, _describe
from uiprobe.pngfile import PngFileError, check_new_path, check_png, save_png
from uiprobe.settings import (
    SETTINGS_FILE,
    IncompleteFileError,
    Settings,
    _shown,
    write_default_settings,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

    from uiprobe.adb import DeviceInfo
    from uiprobe.model import Element, Screen


FIND_SCHEMA = "uiprobe.find/1"


ACTION_SCHEMA = "uiprobe.action/1"


INIT_SCHEMA = "uiprobe.init/1"


SCREENSHOT_SCHEMA = "uiprobe.screenshot/1"


_MAX_COORDINATE = 32767


_COORDINATE_RE = re.compile(r"[0-9]{1,5}")


_MAX_TIMEOUT_TEXT = 32


_TIMEOUT_TEXT = re.compile(r"[0-9]+(?:\.[0-9]+)?")


_DIRECTIONS = ("up", "down", "left", "right")


def _single_serial(args: argparse.Namespace) -> str | None:
    serials: list[str] = [
        *(args.serial or []),
        *(getattr(args, "command_serial", None) or []),
    ]
    if len(serials) > 1:
        msg = "--serial may be given only once"
        raise _UsageError(msg)
    return serials[0] if serials else None


def _device_json(details: DeviceDetails) -> dict[str, object]:
    info = details.info
    entry: dict[str, object] = {"serial": info.serial, "state": info.state}
    if info.model is not None:
        entry["model"] = info.model
    if details.api_level is not None:
        entry["api_level"] = details.api_level
    if details.emulator is not None:
        entry["emulator"] = details.emulator
    return entry


def _devices_text(found: Sequence[DeviceInfo]) -> str:
    rows = [("SERIAL", "STATE", "MODEL")]
    rows.extend((d.serial, d.state, d.model or "") for d in found)
    rows = [tuple(_printable(cell) for cell in row) for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    lines = [f"{a:<{widths[0]}}  {b:<{widths[1]}}  {c}".rstrip() for a, b, c in rows]
    return "\n".join(lines)


def _cmd_devices(args: argparse.Namespace) -> int:
    options = {
        "adb_server": args.adb_server,
        "adb": args.adb,
        "start_server": args.start_server,
    }
    if args.json:
        payload = {
            "schema": "uiprobe.devices/1",
            "devices": [_device_json(d) for d in device_details(**options)],
        }
        _emit(dumps(payload))
    else:
        _emit(_devices_text(devices(**options)))
    return 0


def _cmd_dump(args: argparse.Namespace) -> int:
    if args.brief and args.json:
        msg = "--brief is a text view and cannot be combined with --json"
        raise _UsageError(msg)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    screen = device.dump()
    _emit(screen.to_json(indent=2) if args.json else screen.to_tree(brief=args.brief))
    return 0


def _find_text(screen: Screen, found: list[Element]) -> str:
    noun = "element matches" if len(found) == 1 else "elements match"
    lines = [f"{len(found)} {noun}  ({screen.screen_id})"]
    lines.extend(screen._describe_line(element) for element in found[:MAX_CANDIDATES])  # noqa: SLF001
    if len(found) > MAX_CANDIDATES:
        lines.append(f"... {len(found) - MAX_CANDIDATES} more not shown")
    return "\n".join(lines)


def _cmd_find(args: argparse.Namespace) -> int:
    criteria = {
        key: value
        for key, value in (
            ("text", args.text),
            ("resource_id", args.resource_id),
            ("content_desc", args.desc),
        )
        if value is not None
    }
    if not criteria:
        msg = "give at least one of --text, --id, --desc"
        raise _UsageError(msg)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    screen = device.dump()
    found = screen.find(text=args.text, resource_id=args.resource_id, desc=args.desc)
    if args.json:
        payload = {
            "schema": FIND_SCHEMA,
            "screen_id": screen.screen_id,
            "criteria": criteria,
            "total": len(found),
            "elements": [screen._element_dict(e) for e in found[:MAX_CANDIDATES]],  # noqa: SLF001
        }
        _emit(dumps(payload, indent=2))
    else:
        _emit(_find_text(screen, found))
    return 0


def _parse_xy(values: Sequence[str], option: str = "--xy") -> tuple[int, int]:
    """Read a point option (``--xy``, ``--from``, ``--to``) as two integers.

    Each is a plain decimal integer from 0 to 32767; `option` names the option
    in the error.
    """
    numbers: list[int] = []
    for value in values:
        number = int(value) if _COORDINATE_RE.fullmatch(value) else -1
        if not 0 <= number <= _MAX_COORDINATE:
            msg = (
                f"{option} needs two integers from 0 to {_MAX_COORDINATE}, "
                f"not {value!r}"
            )
            raise _UsageError(msg)
        numbers.append(number)
    return numbers[0], numbers[1]


def _parse_press_ms(values: list[str] | None, *, long: bool) -> int:
    """Read ``--press-ms``: plain decimal integers from 0 to 200, not with --long.

    The option may be repeated; every value must be valid and the last one wins.
    """
    if values is None:
        return DEFAULT_PRESS_MS
    if long:
        msg = "--press-ms cannot be combined with --long"
        raise _UsageError(msg)
    numbers: list[int] = []
    for value in values:
        number = int(value) if re.fullmatch(r"[0-9]{1,3}", value) else -1
        if not 0 <= number <= MAX_PRESS_MS:
            msg = f"--press-ms needs an integer from 0 to {MAX_PRESS_MS}, not {value!r}"
            raise _UsageError(msg)
        numbers.append(number)
    return numbers[-1]


def _cmd_tap(args: argparse.Namespace) -> int:
    selected = (args.text, args.resource_id, args.desc)
    by_element = any(value is not None for value in selected)
    if args.xy is not None and by_element:
        msg = "--xy cannot be combined with --text, --id or --desc"
        raise _UsageError(msg)
    if args.xy is None and not by_element:
        msg = "give --xy X Y, or at least one of --text, --id, --desc"
        raise _UsageError(msg)
    xy = None if args.xy is None else _parse_xy(args.xy)
    long: bool = args.long
    press_ms = _parse_press_ms(args.press_ms, long=long)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    element: Element | None = None
    element_json: dict[str, object] | None = None
    if xy is None:
        # The activity and the API level are not used here: skip their reads.
        screen = device.dump(details=False)
        element = screen.find_one(
            text=args.text, resource_id=args.resource_id, desc=args.desc
        )
        point = screen.tap_point(element)
        element_json = screen._element_dict(element)  # noqa: SLF001
        if "disabled" in element.flags:
            _emit(
                f"uiprobe: warning: {element.id} is disabled; pressing it anyway",
                stream=sys.stderr,
            )
    else:
        point = xy
    pressed = device.tap(point, long=long, press_ms=press_ms)
    verb = "long-tapped" if long else "tapped"
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "long_tap" if long else "tap",
            "point": list(pressed),
        }
        if element_json is not None:
            payload["element"] = element_json
        _emit(dumps(payload))
    else:
        where = f"{pressed[0]},{pressed[1]}"
        if element is None:
            _emit(f"{verb} {where}")
        else:
            _emit(f"{verb} {_printable(_describe(element))} at {where}")
    return 0


def _parse_ms(value: str | None) -> int:
    """Read ``--ms``: a plain decimal integer from 1 to 10000."""
    if value is None:
        return DEFAULT_SWIPE_MS
    number = int(value) if re.fullmatch(r"[0-9]{1,5}", value) else 0
    if not 1 <= number <= _MAX_MS:
        msg = f"--ms needs an integer from 1 to {_MAX_MS}, not {value!r}"
        raise _UsageError(msg)
    return number


def _swipe_arguments(
    args: argparse.Namespace,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None, int, bool]:
    """Check the swipe options; return from, to, duration and by-element."""
    by_element = any(v is not None for v in (args.text, args.resource_id, args.desc))
    if (args.from_xy is None) != (args.to_xy is None):
        msg = "--from and --to must be given together"
        raise _UsageError(msg)
    if args.from_xy is not None and args.direction is not None:
        msg = "--from and --to cannot be combined with --direction"
        raise _UsageError(msg)
    if args.direction is None and by_element:
        msg = "--text, --id and --desc need --direction"
        raise _UsageError(msg)
    if args.direction is None and args.from_xy is None:
        msg = "give --from X Y --to X Y, or --direction up, down, left or right"
        raise _UsageError(msg)
    if args.direction is not None and args.direction not in _DIRECTIONS:
        msg = (
            f"--direction must be one of up, down, left, right, not {args.direction!r}"
        )
        raise _UsageError(msg)
    duration = _parse_ms(args.ms)
    if args.from_xy is None:
        return None, None, duration, by_element
    start = _parse_xy(args.from_xy, "--from")
    end = _parse_xy(args.to_xy, "--to")
    if start == end:
        msg = "--from and --to are the same point; a swipe must move (use tap)"
        raise _UsageError(msg)
    return start, end, duration, by_element


def _cmd_swipe(args: argparse.Namespace) -> int:
    start, end, duration, by_element = _swipe_arguments(args)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    element: Element | None = None
    element_json: dict[str, object] | None = None
    if start is None or end is None:
        if not by_element:
            # Only the display size is needed: skip the full dump.
            try:
                start, end = device.scroll_display(args.direction, duration_ms=duration)
            except DisplaySizeUnknownError:
                device.dump(details=False)
                start, end = device.scroll(args.direction, duration_ms=duration)
        else:
            screen = device.dump(details=False)
            element = screen.find_one(
                text=args.text, resource_id=args.resource_id, desc=args.desc
            )
            element_json = screen._element_dict(element)  # noqa: SLF001
            if "disabled" in element.flags:
                _emit(
                    f"uiprobe: warning: {element.id} is disabled; scrolling it anyway",
                    stream=sys.stderr,
                )
            start, end = device.scroll(
                args.direction, within=element, duration_ms=duration
            )
    else:
        start, end = device.swipe(start, end, duration_ms=duration)
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "swipe",
            "from": list(start),
            "to": list(end),
            "duration_ms": duration,
        }
        if args.direction is not None:
            payload["direction"] = args.direction
        if element_json is not None:
            payload["element"] = element_json
        _emit(dumps(payload))
    else:
        line = f"swiped {start[0]},{start[1]} -> {end[0]},{end[1]} in {duration} ms"
        if element is not None:
            line += f" on {_printable(_describe(element))}"
        _emit(line)
    return 0


def _key_settings(args: argparse.Namespace) -> Settings | None:
    """Return the settings for ``key``, or None to use the built-in lists."""
    _check_config_path(args.config)
    if args.config is not None:
        return _load_settings(args.config)
    if _is_missing(SETTINGS_FILE):
        return None
    return _load_settings(SETTINGS_FILE)


def _cmd_key(args: argparse.Namespace) -> int:
    settings = _key_settings(args)
    refused = (
        None
        if settings is None
        else RefusedKeys(
            frozenset(settings.refused_keys),
            settings.refused_key_prefixes,
            _shown(str(settings.source)),
        )
    )
    _key_code(args.name, refused)  # a bad name is a usage error before any contact
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    sent = device.key(args.name, refused=refused)
    if args.json:
        _emit(dumps({"schema": ACTION_SCHEMA, "action": "key", "key": sent}))
    else:
        _emit(f"pressed key {sent}")
    return 0


def _cmd_type(args: argparse.Namespace) -> int:
    text = str.__str__(args.text)
    _text_pieces(text)  # a bad text is a usage error before any server contact
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    device.type_text(text)
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "type",
            "length": len(text),
            "device": {"serial": device.serial},
            "captured_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        }
        _emit(dumps(payload))
    else:
        _emit(f"typed {len(text)} characters")
    return 0


def _parse_timeout(value: str | None) -> float:
    """Read ``--timeout``: a plain decimal number of seconds from 0 to 600.

    It has at most 32 characters. The range is checked on the exact decimal
    value, so a number just above 600 is refused even when it rounds to 600.
    """
    if value is None:
        return DEFAULT_LAUNCH_TIMEOUT
    shown = value if len(value) <= 40 else value[:40] + "..."
    msg = (
        "--timeout needs a plain decimal number of seconds from 0 to 600 (at"
        f" most {_MAX_TIMEOUT_TEXT} characters), not {shown!r}"
    )
    if len(value) > _MAX_TIMEOUT_TEXT or not _TIMEOUT_TEXT.fullmatch(value):
        raise _UsageError(msg)
    whole, _, fraction = value.partition(".")
    limit = int(MAX_LAUNCH_TIMEOUT)
    if int(whole) > limit or (int(whole) == limit and fraction.strip("0")):
        raise _UsageError(msg)
    return float(value)


def _cmd_launch(args: argparse.Namespace) -> int:
    package = check_package(args.package)
    timeout = _parse_timeout(args.timeout)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    device.launch(package, restart=args.restart, timeout=timeout)
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "launch",
            "package": package,
            "device": {"serial": device.serial},
            "restart": args.restart,
            "captured_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        }
        _emit(dumps(payload))
    else:
        _emit(f"launched {package}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path: str = args.path
    shown = _printable(path)
    if (
        args.serial is not None
        or args.adb is not None
        or args.adb_server is not None
        or not args.start_server
    ):
        msg = (
            "init takes no device options (-s, --adb, --adb-server, --no-start-server)"
        )
        raise _UsageError(msg)
    try:
        write_default_settings(path)
    except FileExistsError:
        msg = f"{shown} already exists; uiprobe never overwrites it"
        raise _UsageError(msg) from None
    except IncompleteFileError as exc:
        msg = (
            f"{shown} was created but could not be written completely:"
            f" {_printable(str(exc))}; the file may be incomplete, so delete it"
            " and run uiprobe init again"
        )
        raise _UsageError(msg) from None
    except (OSError, ValueError) as exc:
        msg = f"{shown} cannot be created: {_printable(str(exc))}"
        raise _UsageError(msg) from None
    if args.json:
        _emit(dumps({"schema": INIT_SCHEMA, "path": path}))
    else:
        _emit(shown)
    return 0


def _check_new_file(path: str) -> None:
    """Refuse what `save_png` would refuse before the device is asked.

    That is an empty path, anything at the path (also a broken link) and, on
    Windows, a path that names a stream of a file.
    """
    shown = _printable(path)
    if not path:
        msg = "-o PATH must not be empty"
        raise _UsageError(msg)
    try:
        check_new_path(path)
    except FileExistsError:
        msg = f"-o {shown}: already exists; give a new file name"
        raise _UsageError(msg) from None
    except ValueError as error:
        msg = f"-o {shown}: {error}"
        raise _UsageError(msg) from None
    except OSError as error:
        msg = f"cannot check {path}: {error.strerror or error}"
        details = {"path": path, "created": False}
        raise PngFileError(msg, details=details) from error


def _cmd_screenshot(args: argparse.Namespace) -> int:
    path = args.output
    _check_new_file(path)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    png = device.screenshot()
    captured_at = datetime.now(UTC).astimezone()
    width, height = check_png(png)
    # Asked before the file is made, so that nothing slow or failing comes
    # between saving and reporting the saved file.
    display = device.display_for_image(width, height) if args.json else None
    try:
        save_png(path, png)
    except FileExistsError:
        msg = f"-o {_printable(path)}: already exists; give a new file name"
        raise _UsageError(msg) from None
    if args.json:
        payload: dict[str, object] = {
            "schema": SCREENSHOT_SCHEMA,
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "device": {"serial": device.serial},
            "path": path,
            "bytes": len(png),
            "width": width,
            "height": height,
        }
        if display is not None:
            payload["display"] = {"width": display[0], "height": display[1]}
        _emit(dumps(payload, indent=2))
    else:
        _emit(f"saved {_printable(path)} ({width}x{height}, {len(png)} bytes)")
    return 0
