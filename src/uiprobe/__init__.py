"""uiprobe: save an Android app's screen structure through the adb server."""

# Also written in pyproject.toml; tests/test_version.py checks that the two match.
__version__ = "0.1.0a1.dev0"

from uiprobe.adb import DeviceInfo
from uiprobe.crawl_driver import CrawlResult, Progress
from uiprobe.crawl_driver import crawl as crawl_run
from uiprobe.crawl_preview import Preview, preview
from uiprobe.device import Device, devices, select_device
from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    DumpError,
    ElementNotFoundError,
    ElementNotVisibleError,
    InputFailedError,
    LaunchFailedError,
    MultipleDevicesError,
    MultipleElementsError,
    NotEmulatorError,
    ScreenshotFailedError,
    UiprobeError,
)
from uiprobe.guards import Policy
from uiprobe.model import Bounds, Element, Screen

__all__ = [
    "AdbError",
    "AdbServerUnavailableError",
    "Bounds",
    "CrawlResult",
    "Device",
    "DeviceInfo",
    "DeviceNotFoundError",
    "DeviceOfflineError",
    "DeviceUnauthorizedError",
    "DumpError",
    "Element",
    "ElementNotFoundError",
    "ElementNotVisibleError",
    "InputFailedError",
    "LaunchFailedError",
    "MultipleDevicesError",
    "MultipleElementsError",
    "NotEmulatorError",
    "Policy",
    "Preview",
    "Progress",
    "Screen",
    "ScreenshotFailedError",
    "UiprobeError",
    "crawl_run",
    "devices",
    "preview",
    "select_device",
]
