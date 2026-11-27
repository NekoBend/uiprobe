"""The reasons, the word lists and the other constants of the guards."""

from __future__ import annotations

import re
from typing import Final, Literal, get_args

Reason = Literal[
    "screen_too_large",
    "outside_target_app",
    "permission_screen",
    "not_visible",
    "covered",
    "disabled",
    "text_input",
    "password_field",
    "unlabeled",
    "number_only",
    "toggle",
    "two_target_row",
    "row_with_button",
    "navigate_up",
    "slider",
    "deny_word",
    "irreversible_screen",
    "confirm_button",
    "dialog_confirm_button",
    "screen_edge",
]

# Every reason, in the fixed order a decision lists them.
REASONS: Final[tuple[Reason, ...]] = get_args(Reason)
# The reasons whose details name the matched deny word or phrase, the field
# and the element (what skipped.jsonl records).
_WORD_REASONS: Final[frozenset[str]] = frozenset(
    {"deny_word", "irreversible_screen", "confirm_button"}
)

# The base list: purchases, payments, sending, deleting, signing out and
# similar (Japanese, then English).
_BASE_DENY_WORDS: Final[tuple[str, ...]] = (
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
    "buy",
    "purchase",
    "pay",
    "checkout",
    "order",
    "subscribe",
    "register",
    "sign up",
    "send",
    "post",
    "share",
    "delete",
    "remove",
    "erase",
    "log out",
    "logout",
    "sign out",
    "signout",
    "unsubscribe",
    "reset",
    "report",
    "block",
    "call",
)
# Not deny words: "redial", "charge", "top up" and "renew" were considered as
# defaults that would keep labels such as "Redial automatically" or "Charge
# automatically" refused next to an exception word. They over-block
# ("Charger", "Stop updates", "Renewal") and are left for a later, more
# careful change; add them in a settings file to use them now.

# Further deny words: subscriptions, sign-off and similar (Japanese, then English).
_EXTRA_DENY_WORDS: Final[tuple[str, ...]] = (
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
    "アップロード",
    "返信",
    "同意",
    "インストール",
    "破棄",
    "クリア",
    "フォーマット",
    "アップグレード",
    "プレミアム",
    "無料体験",
    "チェックアウト",
    "やり直",
    "元に戻",
    "check out",
    "submit",
    "subscription",
    "trial",
    "upgrade",
    "premium",
    "install",
    "uninstall",
    "clear",
    "wipe",
    "format",
    "discard",
    "transfer",
    "withdraw",
    "donate",
    "invite",
    "publish",
    "upload",
    "reply",
    "accept",
    "agree",
    "log off",
    "sign off",
    "restart",
    "undo",
)

# Words for disabling an app, force-stopping it and signing in, such as the
# "Disable" and "Force stop" buttons of an app info page and "Sign in" and
# "Log in" buttons (Japanese first, then English).
_SETTINGS_DENY_WORDS: Final[tuple[str, ...]] = (
    "無効",
    "強制停止",
    "ログイン",
    "サインイン",
    "disable",
    "force stop",
    "sign in",
    "log in",
    "login",
)

# Words for state-changing verbs of Android Settings pages, such as the
# "Forget", "Disconnect" and "Unpair" buttons of a saved network or a paired
# device and "Eject" of a storage device (Japanese first, then English). "restore"
# (and 復元) and 取り消 were considered and left out: "Backup & restore" rows only
# open a sub-page and 取り消し often means "Cancel". "無効化" is not listed: 無効
# above already holds it.
_STATE_DENY_WORDS: Final[tuple[str, ...]] = (
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
)

# Words for carts and accounts, such as "Add to cart" and "My account"
# (Japanese first, then English).
_SHOP_DENY_WORDS: Final[tuple[str, ...]] = (
    "カート",
    "アカウント",
    "cart",
    "account",
)

DEFAULT_DENY_WORDS: Final[tuple[str, ...]] = (
    _BASE_DENY_WORDS
    + _EXTRA_DENY_WORDS
    + _SETTINGS_DENY_WORDS
    + _SHOP_DENY_WORDS
    + _STATE_DENY_WORDS
)

# Built-in exception words (see `word_matches`): none, so the defaults decide
# exactly as substring matching alone. A built-in exception would excuse only
# the built-in deny words; exception words come from the settings file's
# ``[deny] except`` or ``Policy.deny_exceptions``. A word such as
# ``automatically`` (holds ``call``) is a candidate, but it also lets through
# labels such as "Bill my card automatically", so the choice is left to the
# person who writes the settings file.
DEFAULT_DENY_EXCEPTIONS: Final[tuple[str, ...]] = ()

# Phrases that say a screen's action cannot be taken back (English, then
# Japanese). A screen whose text holds one refuses every element
# (``irreversible_screen``). They are matched like the deny words, so spaces
# and separators do not count: "cannot be undone" also matches "can not be
# undone", and "cannot undo" matches "can not undo".
IRREVERSIBLE_PHRASES: Final[tuple[str, ...]] = (
    "can't undo",
    "cannot undo",
    "cannot be undone",
    "can't be undone",
    "cannot be reversed",
    "can't be reversed",
    "cannot be recovered",
    "can't be recovered",
    "irreversible",
    "irreversibly",
    "permanently",
    "this action is permanent",
    "元に戻せません",
    "取り消せません",
    "元に戻すことはできません",
    "元に戻すことができません",
    "取り消すことはできません",
    "取り消すことができません",
    "復元できません",
    "完全に削除",
)

# Confirm words that count on a dialog only (``dialog_confirm_button``): a
# full-screen "Enable" row is an ordinary row. They are part of CONFIRM_WORDS
# (settings and policy carry them like the others), but the screen-wide
# ``confirm_button`` reason ignores them.
DIALOG_ONLY_CONFIRM_WORDS: Final[tuple[str, ...]] = (
    "turn on",
    "enable",
    "オンにする",
    "有効にする",
)

# Labels that are refused on a dialog only, and only as the whole label of a
# button (after the usual clean-up: case, full width, spaces). A label that
# merely starts with one does not count, so "Online", "On-screen", "オンライン"
# and "ログオン" are not touched. They are not confirm words: `Policy` keeps
# them in its own field `dialog_exact_labels`, and a confirm word "on" that a
# caller writes into `confirm_words` keeps the matching of every confirm word.
DIALOG_ONLY_EXACT_LABELS: Final[tuple[str, ...]] = (
    "on",
    "オン",
)

# Labels of buttons that confirm what the screen asks for.
CONFIRM_WORDS: Final[tuple[str, ...]] = (
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
    "yes",
    "confirm",
    "continue",
    "proceed",
    "accept",
    "agree",
    "allow",
    "apply",
    "done",
    *DIALOG_ONLY_CONFIRM_WORDS,
)

# The resource id of the positive button of a platform dialog, ``button1``
# under the ``android`` package or the screen's own package. On a dialog
# screen it is refused whatever its label (``dialog_confirm_button``).
_DIALOG_POSITIVE_ENTRY: Final = "button1"
_DIALOG_POSITIVE_ID: Final = "android:id/button1"

# A screen with more elements than this is not judged: every candidate gets
# ``screen_too_large`` (see `judge`).
MAX_ELEMENTS: Final = 5000

# Packages used to identify system permission dialogs.
PERMISSION_PACKAGES: Final[frozenset[str]] = frozenset(
    {
        "com.android.packageinstaller",
        "com.google.android.packageinstaller",
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
    }
)

# Measured fallback on an API 36 emulator, 320x640 at 160 dpi: the status bar
# is 24 px high (the navigation bar [0,616][320,640] lies inside the bottom
# 32 px), and the gesture areas are 30 px at the left and right edges and 32 px
# at the bottom. The values are pixels, not scaled for other densities.
API36_TOP_PX: Final = 24
API36_BOTTOM_PX: Final = 32
API36_SIDE_PX: Final = 30
_EDGE_SIDES: Final = 4
_MIN_JOINED_PARTS: Final = 2

_CLICKABLE: Final = "clickable"
_DISABLED: Final = "disabled"
_PASSWORD: Final = "password"  # noqa: S105
_CHECKABLE: Final = "checkable"
# The resource id suffix of the divider AOSP Settings draws between the two
# targets of a two-target row (the row and its gear).
_TWO_TARGET_DIVIDER: Final = ":id/two_target_divider"
# The signs of a toolbar's up button (see the ``navigate_up`` rule).
_UP_CLASSES: Final = ("ImageButton", "ImageView")
_TOOLBAR_CLASS: Final = "Toolbar"
_ACTION_BAR_ID: Final = ":id/action_bar"
_UP_IDS: Final = (":id/home", ":id/up")
_NAVIGATE_UP: Final = "Navigate up"
# The class name endings of adjustable controls (see the ``slider`` rule).
_SLIDER_CLASSES: Final = ("SeekBar", "RatingBar", "Slider")
# The crawl's scrolling (see `Policy.scroll_lists`). Every default lives here
# and nowhere else: the policy, the limits, the settings file and `uiprobe init`
# read them.
DEFAULT_SCROLL_LISTS: Final = False
DEFAULT_SCROLLS_PER_SCREEN: Final = 10
# The size of one wheel event (``VSCROLL,-N``) and how many screens may show
# that the wheel changed something before scrolling stops for the run.
DEFAULT_SCROLL_AMOUNT: Final = 3
MAX_SCROLL_AMOUNT: Final = 10
DEFAULT_SCROLL_INCIDENTS: Final = 2
# The wheel scroll works from allow lists, fixed here and not settings, so that a
# settings file cannot extend them. The container (the scrollable element) must
# have exactly one of these classes (exact names, no substrings):
SCROLL_CONTAINER_CLASSES: Final = frozenset(
    {
        "android.widget.ListView",
        "android.widget.ScrollView",
        "androidx.recyclerview.widget.RecyclerView",
        "androidx.core.widget.NestedScrollView",
    }
)
# and every element whose part on the display holds the point must be the
# container itself or have exactly one of these classes (plain layouts and
# labels, which do nothing with a wheel event):
SCROLL_POINT_CLASSES: Final = frozenset(
    {
        "android.widget.LinearLayout",
        "android.widget.FrameLayout",
        "android.widget.RelativeLayout",
        "androidx.constraintlayout.widget.ConstraintLayout",
        "android.view.View",
        "android.view.ViewGroup",
        "android.widget.TextView",
        "android.widget.ImageView",
    }
)
_NUMBER_CATEGORIES: Final = frozenset({"Nd", "Nl", "No"})
_ID_SEPARATOR: Final = ":id/"
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")
_UTF8_BOM: Final = b"\xef\xbb\xbf"
# Categories that carry no readable label: control, format, surrogate, private
# use (icon fonts) and unassigned.
_EMPTY_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
# Unicode Default_Ignorable_Code_Point ranges (DerivedCoreProperties), hard-coded
# so that the module needs no data file. Format characters (Cf) are removed as
# well, see `_is_invisible`.
_DEFAULT_IGNORABLE: Final[tuple[tuple[int, int], ...]] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_TEXT_FIELD_PARTS: Final = ("edittext", "autocompletetextview")
_SEARCH_AUTOCOMPLETE: Final = "searchautocomplete"
_CACHED_VALUE_LENGTH: Final = 256
_SPACES_RE: Final = re.compile(r"\s+")
_SEPARATOR_CHARS: Final = frozenset("_-./:\u2212\u00b7\u30fb\uff65")
# Code point ranges of the Japanese script: kana (with the prolonged sound
# mark and the iteration marks), CJK ideographs (with the ideographic
# iteration mark and closing mark) and half-width katakana. Only letters
# (category L*) in these ranges count, see `_is_japanese_letter`.
_JAPANESE_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3005, 0x3006),  # ideographic iteration mark, closing mark
    (0x3031, 0x3035),  # vertical kana repeat marks
    (0x303B, 0x303C),  # vertical ideographic iteration mark, masu mark
    (0x3041, 0x309F),  # hiragana, with its iteration marks
    (0x30A0, 0x30FF),  # katakana, with the prolonged sound and iteration marks
    (0x31F0, 0x31FF),  # katakana phonetic extensions
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF66, 0xFF9F),  # half-width katakana
    (0x1B000, 0x1B16F),  # kana supplement and extensions
    (0x20000, 0x323AF),  # CJK extensions B to H
)
# Half-width voiced sound marks: letters that NFKC turns into combining marks.
_HALF_WIDTH_VOICED: Final = frozenset("\uff9e\uff9f")


_APOSTROPHES: Final = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201b": "'", "\u02bc": "'", "\u2032": "'"}
)
