"""Read-only preview of what a crawl would press on the screen showing now."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from uiprobe.guards import Policy, judge

if TYPE_CHECKING:
    from uiprobe.device import Device
    from uiprobe.guards import Judgement
    from uiprobe.model import Screen

__all__ = [
    "MAX_PACKAGE_LENGTH",
    "PACKAGE_PATTERN",
    "Preview",
    "check_package",
    "preview",
]

# Android package rule: two or more dot-separated segments, each an ASCII
# letter followed by ASCII letters, digits or underscores; 255 characters at
# most in total (the length is checked apart, see `check_package`).
PACKAGE_PATTERN: Final = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", re.ASCII
)
MAX_PACKAGE_LENGTH: Final = 255


@dataclass(frozen=True)
class Preview:
    """What a crawl would press on one screen.

    ``package`` is the target package, ``screen`` the screen that was judged,
    ``foreground`` the package in front when it was read (None: unknown),
    ``keyboard_shown`` whether the soft keyboard was shown (None: unknown) and
    ``judgement`` one decision per clickable element.
    """

    package: str
    screen: Screen
    foreground: str | None
    keyboard_shown: bool | None
    judgement: Judgement


def check_package(package: object) -> str:
    """Return `package` when it follows the Android package rule.

    Raises:
        TypeError: `package` is not a str.
        ValueError: `package` has fewer than two segments, a segment that does
            not start with an ASCII letter or holds a character other than
            ASCII letters, digits and underscores, or is longer than 255
            characters.
    """
    if not isinstance(package, str):
        msg = f"package must be a str, not {type(package).__name__}"
        raise TypeError(msg)
    if len(package) > MAX_PACKAGE_LENGTH or not PACKAGE_PATTERN.fullmatch(package):
        shown = package if len(package) <= 80 else package[:80] + "..."
        msg = (
            f"not a valid package name: {shown!r} (two or more segments"
            " separated by dots, each an ASCII letter followed by letters,"
            f" digits or underscores; at most {MAX_PACKAGE_LENGTH} characters)"
        )
        raise ValueError(msg)
    return package


def preview(device: Device, package: str, policy: Policy | None = None) -> Preview:
    """Show what a crawl of `package` would press on the screen showing now.

    Nothing is pressed or launched and no file is written on this computer:
    the device sees only the commands of ``Device.dump``, ``Device.foreground``
    and ``Device.keyboard_shown`` (the dump writes its own temporary file on
    the device and tries to remove it afterwards; if the removal fails, the
    file can stay on the device). It acts on whatever is showing now; it does
    not start the app or bring it to the front. When the foreground app is not
    `package` (or is unknown), every candidate gets ``outside_target_app``,
    except on a screen over the element limit, where every candidate gets only
    ``screen_too_large``.

    The reads happen in this order: ``device.dump()``, then
    ``device.foreground()``, then ``device.keyboard_shown()``; then the screen
    is judged with `uiprobe.guards.judge`. `policy` None means ``Policy()``.

    Raises:
        TypeError: `package` is not a str, or `policy` is neither None nor a
            `Policy`.
        ValueError: `package` does not follow the Android package rule (see
            `PACKAGE_PATTERN`; two or more dot-separated segments, at most 255
            characters), or two elements of the screen have the same id.
        UiprobeError: The device reads fail; the errors of `Device.dump`,
            `Device.foreground` and `Device.keyboard_shown` propagate
            unchanged.
    """
    check_package(package)
    if policy is None:
        policy = Policy()
    elif not isinstance(policy, Policy):
        msg = f"policy must be a Policy or None, not {type(policy).__name__}"
        raise TypeError(msg)
    screen = device.dump()
    foreground = device.foreground()
    keyboard = device.keyboard_shown()
    judgement = judge(screen, package, policy, foreground=foreground)
    return Preview(package, screen, foreground, keyboard, judgement)
