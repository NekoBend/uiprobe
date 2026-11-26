"""Shared helpers for the guards tests (screens built by hand)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from uiprobe.guards import (
    Detail,
    Judgement,
    Policy,
    judge,
)
from uiprobe.model import Bounds, Element, Screen

if TYPE_CHECKING:
    import random

PKG = "com.example.shop"


DISPLAY = (320, 640)


CLICK = ("clickable",)


BUTTON = "android.widget.Button"

DIALOG = (20, 200, 300, 440)


def root(bounds=(0, 0, 320, 640), *, package=PKG):
    return Element(
        "e1", None, "android.widget.FrameLayout", Bounds(*bounds), package=package
    )


def el(
    ident,
    *,
    parent="e1",
    text="",
    desc="",
    hint="",
    rid="",
    cls=BUTTON,
    bounds=(50, 100, 250, 150),
    flags=CLICK,
    package=PKG,
):
    return Element(
        ident,
        parent,
        cls,
        Bounds(*bounds),
        text=text,
        resource_id=rid,
        content_desc=desc,
        hint=hint,
        package=package,
        flags=tuple(flags),
    )


def make_screen(elements, *, package=PKG):
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 11, 17, tzinfo=UTC),
        package=package,
        activity=".Main",
        api_level=36,
        display=DISPLAY,
        elements=tuple(elements),
    )


def run(
    elements,
    *,
    policy=None,
    foreground=PKG,
    target=PKG,
    package=PKG,
    root_bounds=(0, 0, 320, 640),
    root_package=PKG,
):
    elements = [root(root_bounds, package=root_package), *elements]
    return judge(
        make_screen(elements, package=package),
        target,
        policy or Policy(),
        foreground=foreground,
    )


def reasons(judgement: Judgement, ident: str):
    for decision in judgement.decisions:
        if decision.element.id == ident:
            return decision.reasons
    msg = f"{ident} is not a candidate"
    raise AssertionError(msg)


def decision(judgement: Judgement, ident: str):
    return next(d for d in judgement.decisions if d.element.id == ident)


SAFE = [
    "Home",
    "Back",
    "Open",
    "Help",
    "Profile",
    "Search",
    "Settings",
    "Menu",
    "listItem",
    "tab_one",
    "ホーム",
    "設定",
    "Photos",
    "OK",
    "Yes",
    "Done",
    "はい",
]


DENY = [
    "Delete",
    "delete account",
    "signUp",
    "sign-up",
    "SIGN UP",
    "Sign\nup",
    "\uff22\uff35\uff39",
    "ﾛｸﾞｱｳﾄ",
    "Log\nOut",
    "log_out",
    "削除",
    "Pay now",
    "購入",
    "\nSend",
    "Signup",
    "LOGOFF",
    "signoff",
    "Sign\u2010up",
    "De\u00adlete",
    "Log\u200bout",
    "Logoff",
    "Si gn up",
    "SignOff",
]


BLANK = [" ", "\u200b", "\ue900", "\u3000\u200e"]


SAFE_IDS = ["com.x:id/icon", "com.x:id/title", "com.example.pay:id/next", ""]


DENY_IDS = [
    "com.x:id/btn_delete",
    "com.x:id/signUpButton",
    "com.x:id/log_out",
    "com.x:id/signupbtn",
]


CLASSES = [
    "android.widget.Button",
    "android.widget.TextView",
    "android.widget.LinearLayout",
    "android.widget.EditText",
    "android.widget.ImageView",
]


def _random_screen(rng: random.Random):
    elements: list[Element] = []
    denies: set[str] = set()

    def pick(safe, deny, blanks=BLANK):
        roll = rng.random()
        if roll < 0.35:
            return "", False
        if roll < 0.45:
            return rng.choice(blanks), False
        if roll < 0.8:
            return rng.choice(safe), False
        return rng.choice(deny), True

    def add(parent, depth):
        ident = f"e{len(elements) + 1}"
        left = rng.randint(-20, 300)
        top = rng.randint(-20, 620)
        bounds = Bounds(
            left, top, left + rng.randint(0, 200), top + rng.randint(0, 200)
        )
        text, d1 = pick(SAFE, DENY)
        desc, d2 = pick(SAFE, DENY)
        hint, d3 = pick(SAFE, DENY)
        rid, d4 = pick(SAFE_IDS, DENY_IDS, ("",))
        flags = tuple(
            flag
            for flag, chance in (
                ("clickable", 0.55),
                ("disabled", 0.1),
                ("password", 0.05),
            )
            if rng.random() < chance
        )
        elements.append(
            Element(
                ident,
                parent,
                rng.choice(CLASSES),
                bounds,
                text=text,
                resource_id=rid,
                content_desc=desc,
                hint=hint,
                package=PKG if rng.random() < 0.9 else "com.other",
                flags=flags,
            )
        )
        if d1 or d2 or d3 or d4:
            denies.add(ident)
        if depth < 3:
            for _ in range(rng.randint(0, 3)):
                if len(elements) < 30:
                    add(ident, depth + 1)

    for _ in range(rng.randint(1, 3)):
        add(None, 0)
    return make_screen(elements), denies


LAYOUT = "android.widget.LinearLayout"


TEXT_VIEW = "android.widget.TextView"


EDIT = "android.widget.EditText"


DIALOG_ROOT = (20, 200, 300, 500)


def _detail(found, ident, reason):
    return next(d for d in decision(found, ident).details if d.reason == reason)


BASE_DENY = [
    *[
        "購入",
        "買う",
        "支払",
        "決済",
        "注文",
        "課金",
        "定期購入",
        "登録",
        "送信",
        "投稿",
        "共有",
        "削除",
        "消去",
    ],
    *[
        "ログアウト",
        "サインアウト",
        "退会",
        "解約",
        "初期化",
        "リセット",
        "通報",
        "ブロック",
        "電話",
        "発信",
    ],
    *["buy", "purchase", "pay", "checkout", "order", "subscribe", "register"],
    "sign up",
    *["send", "post", "share", "delete", "remove", "erase"],
    "log out",
    "logout",
    "sign out",
    "signout",
    *["unsubscribe", "reset", "report", "block", "call"],
]


EXTRA_DENY = [
    *[
        "購読",
        "申込",
        "申し込",
        "予約",
        "契約",
        "送金",
        "振込",
        "振り込",
        "寄付",
        "招待",
        "公開",
    ],
    *["アップロード", "返信", "同意", "インストール", "破棄", "クリア", "フォーマット"],
    *["アップグレード", "プレミアム", "無料体験", "チェックアウト"],
    *["やり直", "元に戻"],
    "check out",
    *[
        "submit",
        "subscription",
        "trial",
        "upgrade",
        "premium",
        "install",
        "uninstall",
        "clear",
    ],
    *[
        "wipe",
        "format",
        "discard",
        "transfer",
        "withdraw",
        "donate",
        "invite",
        "publish",
        "upload",
    ],
    *["reply", "accept", "agree"],
    "log off",
    "sign off",
    "restart",
    "undo",
]


SETTINGS_DENY = [
    *["無効", "強制停止", "ログイン", "サインイン"],
    *["disable", "force stop", "sign in", "log in", "login"],
]


SHOP_DENY = ["カート", "アカウント", "cart", "account"]


STATE_DENY = [
    "切断",
    "接続を解除",
    "ペアリングを解除",
    "オフにする",
    "取り出",
    "マウント解除",
    "リンク解除",
    "forget",
    "disconnect",
    "unpair",
    "turn off",
    "revoke",
    "eject",
    "unmount",
    "unlink",
    "deactivate",
]


EXPECTED_CONFIRM = [
    *[
        "OK",
        "はい",
        "確認",
        "確定",
        "続ける",
        "続行",
        "同意",
        "許可",
        "実行",
        "適用",
        "完了",
    ],
    *[
        "yes",
        "confirm",
        "continue",
        "proceed",
        "accept",
        "agree",
        "allow",
        "apply",
        "done",
        "turn on",
        "enable",
        "オンにする",
        "有効にする",
    ],
]


def _restart_screen(yes: str, no: str = "Wait, cancel!", warning: str = ""):
    """A full-screen confirmation: the title, the text and two buttons."""
    text = "android.widget.TextView"
    return [
        el("e2", cls=text, flags=(), text="Restart game?", bounds=(20, 100, 300, 140)),
        el(
            "e3",
            cls=text,
            flags=(),
            text="Are you sure you want to restart?" + warning,
            bounds=(20, 150, 300, 250),
        ),
        el("e4", text=yes, bounds=(20, 300, 300, 360)),
        el("e5", text=no, bounds=(20, 380, 300, 440)),
    ]


WIFI_SUMMARY = "Wi\u2011Fi doesn't turn back on automatically"


def deny_of(judgement: Judgement, ident: str) -> Detail | None:
    found = decision(judgement, ident)
    return next((d for d in found.details if d.reason == "deny_word"), None)


def t_mobile_row(*, divider: bool = True, gear: bool = True) -> list[Element]:
    """The "T-Mobile" row of AOSP Settings' Internet screen (API 36), as
    recorded: a clickable row with a title, a summary, the two-target divider
    and a widget frame that holds the clickable gear."""
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 206, 320, 285))
    texts = el(
        "e3",
        parent="e2",
        cls="android.widget.RelativeLayout",
        bounds=(72, 206, 215, 285),
        flags=(),
    )
    title = el(
        "e4",
        parent="e3",
        cls="android.widget.TextView",
        rid="android:id/title",
        text="T-Mobile",
        bounds=(72, 222, 148, 250),
        flags=(),
    )
    summary = el(
        "e5",
        parent="e3",
        cls="android.widget.TextView",
        rid="android:id/summary",
        text="5G",
        bounds=(72, 250, 90, 269),
        flags=(),
    )
    middle = el(
        "e6",
        parent="e2",
        cls="android.widget.LinearLayout",
        rid=(
            "com.android.settings:id/two_target_divider"
            if divider
            else "com.android.settings:id/spacer"
        ),
        bounds=(215, 206, 232, 285),
        flags=(),
    )
    frame = el(
        "e7",
        parent="e2",
        cls="android.widget.LinearLayout",
        rid="android:id/widget_frame",
        bounds=(232, 206, 304, 285),
        flags=(),
    )
    button = el(
        "e8",
        parent="e7",
        cls="android.widget.ImageView",
        rid="com.android.settings:id/settings_button",
        desc="Settings",
        bounds=(232, 206, 304, 285),
        flags=CLICK if gear else (),
    )
    return [row, texts, title, summary, middle, frame, button]
