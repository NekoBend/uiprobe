"""Fake app screens whose texts are distinct markers, to look for in a run folder."""

from __future__ import annotations

from tests.crawl_driver.helpers import BUTTON, SHOP
from tests.fake_device import El, Screen

OVERLAY = "com.example.chathead"
WINDOW_MARKER = "MARKwindowtitle"
TEXT_MARKERS = (
    "MARKtextShop",
    "MARKdescShop",
    "MARKhintShop",
    "MARKbuttonText",
    "MARKbuttonDesc",
    "MARKbuttonHint",
    "MARKrowText",
    "MARKpageTitle",
    "MARKdetailTitle",
    "MARKconfirmDesc",
)
HIDDEN_MARKER = "MARKpasswordValue"
HIDDEN_DESC = "MARKpasswordDesc"
HIDDEN_HINT = "MARKpasswordHint"

SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El(
                (0, 0, 320, 100),
                text="MARKtextShop",
                desc="MARKdescShop",
                hint="MARKhintShop",
            ),
            El(
                (20, 200, 300, 240),
                text="MARKbuttonText",
                desc="MARKbuttonDesc",
                hint="MARKbuttonHint",
                cls=BUTTON,
                go="page",
            ),
            El((20, 260, 300, 300), text="MARKrowText", cls=BUTTON, go="page"),
        ),
    ),
    Screen(
        "page",
        f"{SHOP}/.Page",
        (
            El((0, 0, 320, 100), text="MARKpageTitle"),
            El(
                (20, 120, 300, 160),
                text=HIDDEN_MARKER,
                desc=HIDDEN_DESC,
                hint=HIDDEN_HINT,
                cls="android.widget.EditText",
                password=True,
                focusable=True,
                go="page",
            ),
            El((20, 200, 300, 240), text="MARKdetailTitle", cls=BUTTON, go="detail"),
        ),
    ),
    Screen(
        "detail",
        f"{SHOP}/.Detail",
        (El((0, 0, 320, 100), text="MARKconfirmDesc"),),
    ),
)
