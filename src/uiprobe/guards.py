"""No-press rules: which elements of one screen a crawl may press, and why not.

`judge` looks at one captured screen and returns one `Decision` per candidate
(an element with the ``clickable`` flag). A decision lists every reason, in a
fixed order, why the element must not be pressed; an empty list means it may
be pressed. This module alone decides what the crawl never presses, so a miss
is worse than an over-block: when in doubt, the element is not pressed.

The module is pure. It reads nothing from a device or a file; the caller
supplies the foreground package, the screen-edge rectangles (`edge_areas`)
and the deny words (`parse_deny_file` turns the bytes of a deny file into
words; the settings file is what `crawl` reads).

Deny words are matched as substrings ("contains") on a
normalized form of each field (see `normalize`), also with camelCase and the
separators ``_ - . / :`` read as spaces, and also with all spaces removed. So
``signUp``, ``sign-up`` and ``SIGN UP`` all match the word ``sign up``.

Exception words (``Policy.deny_exceptions``; the settings file's ``[deny]
except``) are empty by default (`DEFAULT_DENY_EXCEPTIONS` is ``()``), so the
defaults decide exactly as substring matching alone. With exception words, in
the text, content description and hint, and in the joined texts built from
them, an occurrence of a deny word is not counted when it lies completely
inside an exception word that stands as one whole word of the field: a word
run (a maximal run of letters and digits, `str.isalnum`, with the combining
marks that follow them) of the field after NFKC, with invisible characters
still in place (they end a run), whose case-folded text equals the exception
after NFKC and case folding, whose characters NFKC left unchanged, and next to
which no invisible character hides a longer word. With ``automatically``
listed, ``call`` in ``Automatically`` does not count, and an element holding
the word is pressed unless another deny word or reason refuses it, also
"Bill my card automatically": whoever lists the word takes that on. The deny
words are still found on the normalized forms; each occurrence is traced back
to the characters of the field it comes from and checked on its own: one
occurrence outside every exception run, or one that only overlaps a run,
counts (``Automatically pay``, ``Recorder order``). A run joined across a
space or a separator does not count (``Job order`` holds ``border`` only once
spaces are removed), nor does an inflected form (``Borders``), a run with an
invisible character, a full-width or circled letter, or a combining mark the
exception word does not have (or has differently) in it. A run counts only when
it is a whole word also with the invisible characters around it removed
(``x`` + U+200B + U+0332 + ``recorder`` is one word) and in the original text
before NFKC (U+013F + ``recorder`` is one word; NFKC would split it). In a
joined text (`_Joined`), every hit is traced back to the field values it
comes from, and it may be excused only when the hit and the exception word
around it both lie inside the value of one field, the exception word stands
whole there as above, and in the joined text both of its ends are real
boundaries: a space or punctuation inside the value, or the value's end where
the neighbouring value's adjacent visible character is not a letter, digit or
combining mark (values with only invisible characters are passed over; a mark
at the start of the next value joins). A hit that spans two values, an
exception word that would span two values, and a hit that cannot be traced
are never excused. Hits are found both with the values joined by one space
and with the values joined directly where letters, digits or marks touch
(`_tight_separators`); a hit found in either reading counts unless excused.
So ``recorder`` + ``s``, ``recorder`` + U+0301, U+0332 + ``recorder``,
削除済み + にする and ``Turn on`` + ``automatically`` (with the exception word
listed) are refused, and the Wi-Fi summary of a Settings row stays excused.
When the trace is not certain (NFKC of the characters one by one differs from
NFKC of the field, or a traced form differs from the real one), every
occurrence counts. Resource ids, the irreversibility phrases and the
screen-wide deny search behind ``confirm_button`` never use exception words.
An exception word must be one word run without invisible characters and must
not equal a deny word (any of its forms); `Policy.with_deny_words` drops an
exception equal to an added word.

Which words an exception excuses: an exception word the caller gives excuses
hits of every deny word, built-in or added. A built-in exception word (there
are none at present) would excuse only hits of a built-in deny word, one
equal to an item of `DEFAULT_DENY_WORDS` after `normalize`, so that a word
added with ``--deny`` or in a settings file is never weakened by uiprobe's own
choice. Both are decided by equality, not by where a word came from: a
settings file lists the built-in words itself, and an added word equal to a
built-in one is that built-in word.

The default deny words come from four lists: the base list (purchases,
payments, sending, deleting, signing out and similar), a list of further
actions such as subscriptions and starting over, the nine words for
disabling, force-stopping and signing in (``_SETTINGS_DENY_WORDS``), and the
four words for carts and accounts (``_SHOP_DENY_WORDS``).

Known and accepted over-block: because words are substrings, ``Posture``
matches ``post`` and ``Recorder`` matches ``order``. They are not pressed
(unless the caller lists them as exception words). The package part of a
resource id is not looked at, so ``com.example.pay:id/next`` does not match
``pay``.

Characters that carry no readable label (every Unicode
Default_Ignorable_Code_Point from a hard-coded range table, such as soft
hyphens, U+034F, variation selectors and the Hangul fillers; format
characters, control characters, private use) are removed before matching,
and Unicode dashes and the katakana middle dot count as separators like
``-``. The same removal applies to the label compared with the confirm words,
and surrounding punctuation or symbols (``OK!``, ``Yes.``) are ignored there,
which blocks more, not less.

A label made only of whitespace, format or control characters, or of private
use characters (icon fonts, Unicode category ``Co``) counts as empty, so an
element with only such a label is unlabeled.

Two crawl rules, each a separate check that a
`Policy` field turns off:

* ``number_only`` (``Policy.skip_number_only``): the label of the candidate is
  made only of numerals and of whitespace, punctuation and symbols (Unicode
  categories N, Z, P and S), with at least one numeral. Numerals are every
  character of the Unicode number categories (Nd, Nl, No): decimal digits of
  any script, but also fractions such as ``½`` and Roman numerals such as
  ``Ⅳ``, which are refused on purpose (a miss is worse than an over-block).
  The label is the
  candidate's own text, else its content description, else its hint, else the
  first non-empty text or content description of its descendants in document
  order; invisible characters are removed first. ``200``, ``$5``, ``1,000``,
  ``12:30`` and ``+1`` are refused; ``8:30 AM``, ``Page 2`` and ``2 items``
  are not. The reason is that such a label is often a price or amount (a shop
  tile "200" spends in-game coins) or a keypad digit. Known over-block: page
  number buttons, keypads and rating stars labelled ``4.5`` are not pressed.
* ``toggle`` (``Policy.skip_toggles``): the candidate or any element in its
  subtree has the ``checkable`` flag (switches, checkboxes, radio buttons,
  toggle buttons), because pressing one changes an app setting. Known
  over-block: a Settings row whose main press opens a sub-screen but which
  also holds a switch is not pressed either.

Both rules only add refusals. In `REASONS` they come right after
``unlabeled``: ``number_only`` first, then ``toggle``.

A third rule works on the whole screen: ``irreversible_screen``
(``Policy.skip_irreversible``). When the text, content_desc or hint of any
element holds one of ``Policy.irreversible_phrases`` (`IRREVERSIBLE_PHRASES`:
"can't undo", "cannot be undone", "permanently", "元に戻せません", ...), every
candidate of the screen gets the reason, whatever its own label is, because
a screen that warns about an action that cannot be taken back is not a place
to press anything. A phrase matches as a substring after the same
normalization as the deny words (NFKC, case folding, invisible characters
removed, separators ignored) and with curly apostrophes mapped to a plain one,
so "You CAN'T undo this" matches, and so does the same text with a curly
apostrophe. The fields of each element are matched alone, in document order
(text, content_desc, hint; the resource id is not read); a phrase split over
two elements is not found. In `REASONS` the reason comes right after
``deny_word``. Known over-block: ``permanently`` also matches
"Permanently moved" and a settings text such as "Permanently allowed".

Choices that block more than strictly needed:

* The deny scope of a candidate holds every clickable ancestor's whole
  subtree, not only the nearest one's.
* A candidate is a confirm button when the confirm form of any of these is a
  confirm word: its own text, content_desc and hint, the text, content_desc
  and hint of any descendant, and the descendants' texts (and, likewise,
  content_descs) joined by one space in document order, also joined with no
  space where one text ends and the next starts with a letter of the
  Japanese script (``確`` + ``認してください``). ``Book`` is not a confirm
  word.
* A confirm form also counts when it starts with a confirm word followed by a
  boundary: ``Yes I'm sure!``, ``OK, delete``, ``Yes, continue`` and
  ``Continue anyway`` are confirm labels, ``Okinawa``, ``Yesterday`` and
  ``Continued`` are not (the word is only the start of a longer word). The
  boundary is a character that is not a letter (a space, punctuation or a
  digit), or the end of the form. A confirm word that ends in a letter of
  the Japanese script (hiragana, katakana with the prolonged sound mark
  ``ー`` and the iteration marks, kanji with ``々``) needs no boundary,
  because Japanese is written without spaces: ``はい、削除します`` and
  ``はい削除します`` both start with ``はい``, and ``オーケーです`` starts
  with ``オーケー``. The cost is over-blocking, for example ``はいる``,
  ``確認画面`` and ``許可しない``. Every other script keeps the boundary
  (a Hebrew or Korean word is not matched at the start of a longer word),
  and a confirm word in Latin letters is not matched when Japanese follows
  it (``OKです``).
* Whitespace, punctuation and symbols are trimmed from both ends of a confirm
  label until nothing changes (``OK!``, ``( OK )``, ``[ OK ]``), and custom
  confirm words go through the same cleaning as the label.
* Unicode dashes (category ``Pd``) and middle dots are separators.
* Format characters (category ``Cf``) and the other default-ignorable code
  points are removed before matching.
* ``Policy.avoid`` defaults to None, meaning ``api36_fallback_areas`` of the
  screen's display at judge time; an explicit tuple, including ``()``, is used
  as given. ``Policy.edges`` (top, bottom, left, right sizes) replaces the API
  36 sizes when ``avoid`` is None.
* Duplicate element ids on one screen raise `ValueError`.
* Control characters in a deny file are rejected, comment lines included.
* A screen with more than `MAX_ELEMENTS` (5000) elements is not judged: every
  candidate gets ``screen_too_large`` and nothing else.

Visibility and cover: an element is visible only when the part of it on the
display is at least 8 pixels wide and 8 pixels high (``MIN_VISIBLE_PX`` of
`uiprobe.model`; ``Policy.min_visible_px`` changes the number for the judging
only); a thinner sliver is ``not_visible``. A candidate that is
visible is ``covered`` when the topmost meaningful element (clickable, or with
a non-empty text, content description or hint; the last one in document order)
whose part on the display holds the press point (whatever its size, a thin
sliver on top counts) is neither the candidate nor one of
its descendants. This is the rule of ``Screen.tap_point``. The reasons come in
the order of `REASONS`: ``screen_too_large``, ``outside_target_app``,
``permission_screen``, ``not_visible``, ``covered``, ``disabled``,
``text_input``, ``password_field``, ``unlabeled``, ``number_only``,
``toggle``, ``deny_word``, ``irreversible_screen``, ``confirm_button``,
``dialog_confirm_button``, ``screen_edge``.

Hit targets: the hit targets of a candidate are the elements whose visible part
holds its press point (the candidate, its ancestors and every overlay). A
candidate is refused when a hit target is in another package or a permission
package, is a text field, carries the password flag, or, when clickable, is a
confirm button or has a password descendant. So a button under a password
field, a permission button or an "OK" button is refused at the same point
where the element on top is. The detail names the hit target.

Deny scope of a candidate: its own subtree and the subtrees of its clickable
ancestors, plus, for each hit target, that element's own fields, its whole
subtree if it is clickable, and the subtrees of its clickable ancestors. A
non-clickable element that contains the point (a layout container, the root)
adds only its own fields, so one deny word elsewhere on a normal screen does
not block everything.

Besides each field alone, deny words are matched against the joined text of the
subtree of each clickable element in the scope: the candidate, its clickable
ancestors and the clickable hit targets. Three strings are matched, each
joined by one space in document order: the texts and content_descs together,
the texts alone and the content_descs alone, so a description between two
parts of ``Log`` + ``out`` does not hide ``log out``. The detail has field
``joined`` and names the subtree root. Without exception words, a deny word in
the joined text of a subtree is also in that of every clickable ancestor, so
only the highest clickable element above each element is matched. With
exception words that does not hold (a longer exception word can form in the
ancestor's joined text), so the joined text of every clickable element in the
highest clickable subtree is matched too, and a hit in any of them refuses
(`_Judge.joined_root`).

For ``confirm_button`` the whole screen counts: the deny words are matched in
each field alone, then in the joined texts, the joined content_descs and the
joined text and content_desc sequence of all elements in document order. The
exception words are not used there, so a deny word inside an exception word
anywhere on the screen still refuses a confirm button.

Cost: the work grows linearly with the number of elements. One pass over the
tree finds, for every element, the first deny hit in its own fields and in its
subtree; hit targets come from a grid over the display; both are cached per
element and per press point. The joined text is matched once per highest
clickable root, so nested clickables do not make it quadratic; with exception
words it is matched once per clickable element, which grows with the nesting
depth of clickables. The one
exception is the confirm-label search (_find_label), which can be quadratic in
the nesting depth of clickable chains (deeper than real layouts).

Known limits:

* Known effect: the own fields (including resource_id) of every
  non-clickable element holding the press point are in scope, so a full-screen
  container whose content_desc or resource id holds a deny word (for example a
  RecyclerView with id ``order_history_list``) blocks every candidate under
  it. This over-block is accepted: a miss is worse than an over-block.
* A deny label that is a sibling of a clickable icon inside a non-clickable
  row is not in the icon's scope.
* A password field below a non-clickable container that holds the press point
  does not refuse the candidate (the container is not pressed); only the
  candidate, its clickable hit targets and a hit target that is itself a
  password field count.
* The words ``restart`` and ``undo`` (やり直, 元に戻) are deny words, so an
  ``Undo`` button that only reverts the last edit is refused too, and so is a
  ``Restart`` button of a media player. Because they match as substrings,
  ``Restarted``, ``Undone``, ``Undoubtedly`` and ``Undock`` are refused as
  well. All of these are accepted:
  a miss is worse than an over-block.
* Half-width or full-width katakana ``ハイ`` (``ﾊｲ``) is not a confirm word,
  because the confirm list has only ``はい``.
* Substring matching over-blocks: ``Posture`` contains ``post`` and
  ``Recorder`` contains ``order``.
* An exception word lets through every label in which it stands whole and no
  other deny word matches, including labels that name an action: with
  ``automatically`` listed, ``Bill my card automatically``, ``Dial
  automatically`` and ``Redial automatically`` are pressable.
* Accepted limits of exception words in joined texts and folding: a
  compatibility character that NFKC turns into a letter at the start of the
  next part is not seen as continuing the word (``recorder`` + U+24E2), a
  combining mark at the start of a part after punctuation is passed over
  (``!`` + U+0332 + ``recorder``), and case folding can create the hit that
  the exception excuses (``reaßigning`` with ``reassigning`` listed). The
  other way round, a neighbouring part that NFKC changes makes the whole
  reading untraceable, and every hit in it counts.
* The nine words for disabling, force-stopping and signing in (``disable``,
  ``force stop``, ``sign in``, ``log in``, ``login``, 無効, 強制停止, ログイン,
  サインイン)
  over-block too: ``sign in`` matches ``Designing`` and ``Signing out`` once
  spaces are removed, and ``log in`` and
  ``sign in`` match any word ending in ``log`` or ``sign`` followed by a word
  starting with ``in`` the same way
  (``Log info``, ``Dialog input``, ``Catalog in stock``, ``Design info``,
  ``Sign inbox``); ログイン also matches inside ブログインポート.
  ``disable`` matches a ``Disabled`` status text, and 無効 matches a status
  text such as ``Wi-Fi 無効`` in a Settings row. 無効 does not match
  ``Disabled``. All of these are accepted: a miss is worse than an
  over-block.
* The four words for carts and accounts (``カート``, ``アカウント``,
  ``cart``, ``account``) over-block too: ``account`` matches ``Accounts`` in
  Settings and ``Accounting``; ``cart`` matches ``Cartoon``, ``Cartridge`` and
  ``Descartes``; カート matches ``カートゥーン``;
  アカウント matches ``アカウント情報``. With spaces removed, ``cart`` also
  matches a word ending in ``c`` followed by a word starting with ``art``
  (``Music artists``, ``Comic art``) and ``account`` matches ``Ac count``.
  Exception words do not help where the hit spans two words (``Dialog
  input``, ``Catalog in stock``, ``Music artists``). All of these are
  accepted: a miss is worse than an over-block.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, get_args

from uiprobe.model import MIN_VISIBLE_PX, Bounds

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from uiprobe.model import Element, Screen

__all__ = [
    "API36_BOTTOM_PX",
    "API36_SIDE_PX",
    "API36_TOP_PX",
    "CONFIRM_WORDS",
    "DEFAULT_DENY_EXCEPTIONS",
    "DEFAULT_DENY_WORDS",
    "IRREVERSIBLE_PHRASES",
    "MAX_ELEMENTS",
    "PERMISSION_PACKAGES",
    "REASONS",
    "Decision",
    "Detail",
    "Judgement",
    "Policy",
    "Reason",
    "api36_fallback_areas",
    "deny_word_in",
    "edge_areas",
    "judge",
    "normalize",
    "parse_deny_file",
    "word_matches",
]

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

# Words for carts and accounts, such as "Add to cart" and "My account"
# (Japanese first, then English).
_SHOP_DENY_WORDS: Final[tuple[str, ...]] = (
    "カート",
    "アカウント",
    "cart",
    "account",
)

DEFAULT_DENY_WORDS: Final[tuple[str, ...]] = (
    _BASE_DENY_WORDS + _EXTRA_DENY_WORDS + _SETTINGS_DENY_WORDS + _SHOP_DENY_WORDS
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
)

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


# -- normalization and matching ------------------------------------------------


def normalize(value: str) -> str:
    """Return NFKC, case-folded, with every whitespace run made one space.

    NFKC maps full-width Latin letters to ASCII and half-width katakana to
    the full-width forms. The result has no leading or trailing space.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _SPACES_RE.sub(" ", folded).strip()


def _is_default_ignorable(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _DEFAULT_IGNORABLE)


def _is_invisible(char: str) -> bool:
    """True for a character that carries no readable label (not whitespace)."""
    if char.isspace():
        return False
    return unicodedata.category(char) in _EMPTY_CATEGORIES or _is_default_ignorable(
        char
    )


def _strip_invisible(value: str) -> str:
    """Remove characters that carry no readable label (whitespace is kept)."""
    return "".join(char for char in value if not _is_invisible(char))


def _separators_to_spaces(value: str) -> str:
    return "".join(
        " " if char in _SEPARATOR_CHARS or unicodedata.category(char) == "Pd" else char
        for char in value
    )


def _split_camel(value: str) -> str:
    """Insert a space at camelCase boundaries (``signUp``, ``HTMLParser``)."""
    out: list[str] = []
    for index, char in enumerate(value):
        if index and char.isupper():
            prev = value[index - 1]
            following = value[index + 1 : index + 2]
            if prev.islower() or (prev.isupper() and following.islower()):
                out.append(" ")
        out.append(char)
    return "".join(out)


def _form_triple(value: str) -> tuple[str, str, str]:
    """Return the normalized, spaced and joined forms of a value, in order."""
    visible = _strip_invisible(unicodedata.normalize("NFKC", value))
    plain = normalize(visible)
    spaced = normalize(_separators_to_spaces(_split_camel(visible)))
    joined = "".join(spaced.split(" "))
    return plain, spaced, joined


def _forms(value: str) -> tuple[str, ...]:
    """Return the distinct normalized, spaced and joined forms of a value."""
    # The spaced form is redundant with plain + joined (removing spaces on
    # both sides keeps a substring a substring); it is kept for clarity.
    return tuple(dict.fromkeys(_form_triple(value)))


def _is_word_char(char: str) -> bool:
    """True for a letter, a digit or a combining mark."""
    return char.isalnum() or unicodedata.category(char)[0] == "M"


def _visible_end(text: str, *, last: bool) -> str:
    """Return the first (or last) visible character of `text`, or ``""``."""
    chars = reversed(text) if last else iter(text)
    return next((c for c in chars if not _is_invisible(c)), "")


def _tight_separators(parts: Sequence[str]) -> list[str]:
    """Return the separator put before each part in the tight reading.

    The space between two parts is uiprobe's, not the app's. Where the last
    visible character before it and the first visible character after it
    are both letters, digits or combining marks, in any script, the parts
    are joined directly (``recorder`` + ``s`` reads ``recorders``,
    ``削除済み`` + ``にする`` reads ``削除済みにする``). A part with no visible
    character is joined directly and does not count as either side, so
    ``削除済み`` + U+200B + ``にする`` reads as one word too, and a first part
    with no visible character adds no space before the next one.
    """
    separators: list[str] = []
    last = ""
    for part in parts:
        first = _visible_end(part, last=False)
        if not separators:
            separators.append("")
        elif first and last and not (_is_word_char(last) and _is_word_char(first)):
            separators.append(" ")
        else:
            separators.append("")
        tail = _visible_end(part, last=True)
        if tail:
            last = tail
    return separators


def _join(parts: Sequence[str], separators: Sequence[str]) -> tuple[str, list[int]]:
    """Join the parts with the separators; return the text and where each
    part starts in it."""
    out: list[str] = []
    starts: list[int] = []
    length = 0
    for separator, part in zip(separators, parts, strict=True):
        out.append(separator)
        length += len(separator)
        starts.append(length)
        out.append(part)
        length += len(part)
    return "".join(out), starts


def _alone_in_join(raw: str, start: int, end: int, part: tuple[int, int]) -> bool:
    """True when ``raw[start:end]``, an exception run inside the value at
    ``raw[part[0]:part[1]]``, has real boundaries in the values written one
    after another (`raw`, with no separator added).

    Inside the value, the run already stands alone (`_Excuser`). Where the
    run reaches the start of the value (past invisible characters and
    combining marks) or its end (past invisible characters), the nearest
    visible character of the neighbouring values must not be a letter, a
    digit or a combining mark; values with only invisible characters are
    passed over.
    """
    first, last = part
    before = start - 1
    while before >= first and (
        _is_invisible(raw[before]) or unicodedata.category(raw[before])[0] == "M"
    ):
        before -= 1
    if before < first:
        before = first - 1
        while before >= 0 and _is_invisible(raw[before]):
            before -= 1
        if before >= 0 and _is_word_char(raw[before]):
            return False
    after = end
    while after < last and _is_invisible(raw[after]):
        after += 1
    if after >= last:
        after = last
        while after < len(raw) and _is_invisible(raw[after]):
            after += 1
        if after < len(raw) and _is_word_char(raw[after]):
            return False
    return True


def _is_japanese_letter(char: str) -> bool:
    """True for a letter of the Japanese script (kana, ``ー``, ``々``, kanji)."""
    if not unicodedata.category(char).startswith("L"):
        return False
    code = ord(char)
    return any(low <= code <= high for low, high in _JAPANESE_RANGES)


def _phrase_key(phrase: str) -> str:
    """Return what decides which texts an irreversibility phrase matches.

    The phrase normalized with curly apostrophes read as plain ones and with
    every space and separator removed: the matching always compares this
    form with the same form of the text, and every other form that matches
    implies that this one does. Two phrases with the same key match the
    same texts.
    """
    visible = _strip_invisible(
        unicodedata.normalize("NFKC", phrase.translate(_APOSTROPHES))
    )
    spaced = normalize(_separators_to_spaces(_split_camel(visible)))
    return "".join(spaced.split(" "))


def _check_word(word: str) -> str:
    if not normalize(word) or not _has_content(word):
        msg = f"word is empty after normalization: {word!r}"
        raise ValueError(msg)
    return word


def word_matches(value: str, word: str, *, exceptions: Iterable[str] = ()) -> bool:
    """Return True if the deny `word` matches the field `value`.

    The word matches when one of its forms is a substring of a form of the
    value (see the module docstring). With `exceptions` (exception words such
    as ``("automatically",)``), an occurrence of the word that lies
    completely inside a whole word of `value` equal to an exception word does
    not count; every other occurrence still does (see `_Excuser`). This is
    the rule of the text, content description and hint fields and of the
    joined texts; a resource id is matched without exceptions.

    A built-in exception word (equal to one of `DEFAULT_DENY_EXCEPTIONS`,
    which is empty) would apply only when `word` is a built-in deny word
    (equal to one of `DEFAULT_DENY_WORDS` after `normalize`); any other
    exception word applies to every deny word.

    Raises:
        TypeError: `exceptions` is one string, not a collection of words.
        ValueError: The word is empty after normalization, an exception word
            is not one word of letters and digits (or holds an invisible
            character), or an exception word equals `word` (see `Policy`).
    """
    _check_word(word)
    exceptions = tuple(_check_collection(exceptions))
    excuser = _Excuser(_exception_keys(exceptions))
    _check_no_conflict(exceptions, (word,))
    word_forms = _forms(word)
    if not _forms_match(word_forms, _forms(value)):
        return False
    return excuser.counts(value, word_forms, builtin=_is_builtin_word(word))


def _forms_match(word_forms: tuple[str, ...], field_forms: tuple[str, ...]) -> bool:
    return any(w in f for w in word_forms if w for f in field_forms)


# -- exception words ---------------------------------------------------------------


def _in_word(char: str, *, inside: bool) -> bool:
    """True for a character that belongs to a word run.

    A run is a maximal sequence of letters and digits (`str.isalnum`) with the
    combining marks (category M) that follow them; a mark starts no run.
    """
    return char.isalnum() or (inside and unicodedata.category(char)[0] == "M")


def _word_runs(text: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` index pairs of the word runs of `text`."""
    runs: list[tuple[int, int]] = []
    start = -1
    for index, char in enumerate(text):
        if _in_word(char, inside=start != -1):
            if start == -1:
                start = index
        elif start != -1:
            runs.append((start, index))
            start = -1
    if start != -1:
        runs.append((start, len(text)))
    return runs


def _is_combining(char: str) -> bool:
    return unicodedata.category(char)[0] == "M"


def _stands_alone(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is a whole word even with the invisible
    characters around it removed.

    Deny matching removes invisible characters, so ``recorder`` + U+200B +
    ``s`` reads as ``recorders`` and ``x`` + U+200B + U+0332 + ``recorder``
    as one word. Before the span, invisible characters and combining marks
    are passed over: a letter or digit found there joins the span. After it,
    invisible characters are passed over: a letter, digit or combining mark
    found there joins the span.
    """
    before = start - 1
    while before >= 0 and (_is_invisible(text[before]) or _is_combining(text[before])):
        before -= 1
    if before >= 0 and text[before].isalnum():
        return False
    after = end
    while after < len(text) and _is_invisible(text[after]):
        after += 1
    return not (
        after < len(text) and (text[after].isalnum() or _is_combining(text[after]))
    )


def _exception_key(word: str) -> str:
    """Return the form a word run is compared with: NFKC, then case-folded.

    Raises:
        ValueError: The word is empty after normalization, holds an invisible
            character (a default-ignorable one such as U+200B or the
            variation selector U+FE0F, or a format or control character:
            deny matching removes them, so the exception could equal a deny
            word in disguise), or is not one word of letters and digits (a
            space, a separator or punctuation in it would keep it from ever
            being equal to a word of a text).
    """
    _check_word(word)
    if any(_is_invisible(char) for char in word):
        msg = f"exception word holds an invisible character: {word!r}"
        raise ValueError(msg)
    folded = unicodedata.normalize("NFKC", word)
    if _word_runs(folded) != [(0, len(folded))]:
        msg = f"exception word must be one word of letters and digits: {word!r}"
        raise ValueError(msg)
    return folded.casefold()


def _is_builtin_word(word: str) -> bool:
    """True for a deny word equal to a built-in one after `normalize`.

    Only such a word is weakened by the built-in exception words.
    """
    return normalize(word) in _BUILTIN_WORD_KEYS


def _check_collection(exceptions: Iterable[str]) -> Iterable[str]:
    if isinstance(exceptions, str):
        msg = "exception words must be a collection of words, not one string"
        raise TypeError(msg)
    return exceptions


def _exception_keys(exceptions: Iterable[str]) -> frozenset[str]:
    return frozenset(_exception_key(word) for word in _check_collection(exceptions))


def _check_no_conflict(exceptions: Iterable[str], deny_words: Iterable[str]) -> None:
    """Raise when an exception word equals a deny word (see `Policy`).

    Raises:
        ValueError: An exception word equals a deny word (any of its forms).
    """
    deny_forms = _deny_forms(deny_words)
    for exception in exceptions:
        word = _exception_conflict(exception, deny_forms)
        if word is not None:
            msg = (
                f"exception word {exception!r} equals the deny word {word!r};"
                " an exception cannot excuse its own deny word"
            )
            raise ValueError(msg)


# What makes a deny word built-in (`_is_builtin_word`) and an exception word
# built-in (its key is one of these): built-in exceptions excuse only hits of
# built-in deny words.
_BUILTIN_WORD_KEYS: Final = frozenset(normalize(word) for word in DEFAULT_DENY_WORDS)
# The same as `_exception_key` of each word (a test checks it); that function
# needs helpers defined further down.
_BUILTIN_EXCEPTION_KEYS: Final = frozenset(
    unicodedata.normalize("NFKC", word).casefold() for word in DEFAULT_DENY_EXCEPTIONS
)


def _deny_forms(words: Iterable[str]) -> dict[str, str]:
    """Return every form (see `_forms`) of the deny words, each with its word."""
    found: dict[str, str] = {}
    for word in words:
        for form in _forms(word):
            found.setdefault(form, word)
    return found


def _exception_conflict(exception: str, deny_forms: dict[str, str]) -> str | None:
    """Return the deny word that the exception word equals, or None.

    `deny_forms` comes from `_deny_forms`. Equal means that one of the deny
    word's forms is the exception's key, so that every whole-word hit of that
    deny word would be excused.
    """
    return deny_forms.get(_exception_key(exception))


def _fold_traced(pairs: Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    """Case-fold, make every whitespace run one space and strip, with sources."""
    out: list[tuple[str, int]] = []
    for char, source in pairs:
        for folded in char.casefold():
            if not folded.isspace():
                out.append((folded, source))
            elif out and out[-1][0] != " ":
                out.append((" ", source))
    if out and out[-1][0] == " ":
        out.pop()
    return out


def _traced_forms(text: str) -> list[tuple[str, list[int]]] | None:
    """Return the three forms of an NFKC text, each with the index in `text`
    of the character that every form character comes from.

    The forms are built character by character the way `_form_triple` builds
    them; None when the result differs from `_form_triple` (for example when
    removing an invisible character lets NFKC compose two characters), so
    that the caller can count every hit.
    """
    visible = [
        (char, index) for index, char in enumerate(text) if not _is_invisible(char)
    ]
    camel: list[tuple[str, int]] = []
    for position, (char, index) in enumerate(visible):
        if position and char.isupper():
            prev = visible[position - 1][0]
            following = visible[position + 1][0] if position + 1 < len(visible) else ""
            if prev.islower() or (prev.isupper() and following.islower()):
                camel.append((" ", index))
        camel.append((char, index))
    separated = [
        (
            " "
            if char in _SEPARATOR_CHARS or unicodedata.category(char) == "Pd"
            else char,
            index,
        )
        for char, index in camel
    ]
    plain = _fold_traced(visible)
    spaced = _fold_traced(separated)
    joined = [pair for pair in spaced if pair[0] != " "]
    traced = [
        ("".join(char for char, _ in pairs), [index for _, index in pairs])
        for pairs in (plain, spaced, joined)
    ]
    if tuple(form for form, _ in traced) != _form_triple(text):
        return None
    return traced


def _hit_outside(
    form: str, sources: list[int], word_form: str, runs: list[tuple[int, int]]
) -> bool:
    """True when an occurrence of `word_form` in `form` is outside every run.

    `runs` are disjoint and in text order, and the sources of later
    occurrences do not start earlier (the traced forms keep text order), so
    one index moves forward through the runs: the work is linear in the
    number of occurrences plus runs. An occurrence that starts before the
    current run counts, which can only block more.
    """
    index = 0
    start = form.find(word_form)
    while start != -1:
        span = sources[start : start + len(word_form)]
        low, high = min(span), max(span)
        while index < len(runs) and runs[index][1] <= low:
            index += 1
        if index == len(runs) or not runs[index][0] <= low or high >= runs[index][1]:
            return True
        start = form.find(word_form, start + 1)
    return False


# The traced forms of a field (`_traced_forms`) and its exception runs, each
# as ``(start, end, builtin)``: builtin is True when the run equals a built-in
# exception word.
_Traced = tuple[list[tuple[str, list[int]]], list[tuple[int, int, bool]]]


class _Excuser:
    """Decides whether a deny-word hit in a field lies inside an exception word.

    An exception applies only where it is one whole word of the field: a word
    run (see `_word_runs`) of the field after NFKC, with the invisible
    characters still in place (they end a run), whose case-folded text equals
    the key of an exception word, and whose characters NFKC left unchanged
    (a circled or full-width letter in the run fails it). The deny words are
    found on the normalized forms of the field (`_form_triple`); every hit is
    traced back to the characters it comes from, and it is excused only when
    all of them lie inside one such run. Every hit is checked on its own, so
    one hit outside every exception run counts. When tracing is not certain
    (NFKC of the characters one by one differs from NFKC of the whole field,
    or a traced form differs from the real one), every hit counts. A run that
    equals a built-in exception word (`DEFAULT_DENY_EXCEPTIONS`) excuses only
    hits of a built-in deny word; a run equal to any other exception word
    excuses hits of every deny word.
    """

    def __init__(self, keys: frozenset[str]) -> None:
        self.keys = keys
        self._cache: dict[str, _Traced | None] = {}
        # The last long value (not cached): a joined text is matched against
        # every deny word that hits it.
        self._last: tuple[str, _Traced | None] | None = None

    def counts(self, value: str, word_forms: tuple[str, ...], *, builtin: bool) -> bool:
        """Return True when a hit of the word forms in `value` is not excused.

        `builtin` tells whether the deny word is a built-in one
        (`_is_builtin_word`); otherwise the built-in exception words do not
        excuse it. The caller has already found at least one hit.
        """
        traced = self._trace(value)
        if traced is None:
            return True
        forms, tagged = traced
        runs = [(first, end) for first, end, own in tagged if builtin or not own]
        if not runs:
            return True
        for form, sources in forms:
            for word_form in word_forms:
                if word_form and _hit_outside(form, sources, word_form, runs):
                    return True
        return False

    def _trace(self, value: str) -> _Traced | None:
        """Return the traced forms and the exception runs, or None when no
        hit can be excused (no exception run, or tracing is not certain).
        """
        if value in self._cache:
            return self._cache[value]
        if self._last is not None and self._last[0] == value:
            return self._last[1]
        result = self._compute(value)
        if len(value) <= _CACHED_VALUE_LENGTH:
            self._cache[value] = result
        else:
            self._last = (value, result)
        return result

    def _compute(self, value: str) -> _Traced | None:
        if not self.keys:
            return None
        traced = _nfkc_traced(value)
        if traced is None:
            return None
        text, unchanged, origin = traced
        runs = self._runs(value, text, unchanged, origin)
        if not runs:
            return None
        forms = _traced_forms(text)
        if forms is None:
            return None
        return forms, runs

    def _runs(
        self, value: str, text: str, unchanged: list[bool], origin: list[int]
    ) -> list[tuple[int, int, bool]]:
        """Return the exception runs of `text` (the NFKC of `value`).

        A run counts only when it is a whole word both after NFKC and in the
        original text: NFKC can create a boundary (U+013F becomes "L" and a
        middle dot), and invisible characters can hide a longer word. The
        characters of a counted run are unchanged by NFKC, so the run is the
        same span of the original text.
        """
        return [
            (start, end, text[start:end].casefold() in _BUILTIN_EXCEPTION_KEYS)
            for start, end in _word_runs(text)
            if text[start:end].casefold() in self.keys
            and all(unchanged[start:end])
            and _stands_alone(text, start, end)
            and _stands_alone(value, origin[start], origin[end - 1] + 1)
        ]

    def part_runs(self, value: str) -> list[tuple[int, int, bool]]:
        """Return the exception runs of one field value as spans of the value
        itself (before NFKC); none when tracing is not certain."""
        if not self.keys:
            return []
        traced = _nfkc_traced(value)
        if traced is None:
            return []
        text, unchanged, origin = traced
        return [
            (origin[start], origin[end - 1] + 1, builtin)
            for start, end, builtin in self._runs(value, text, unchanged, origin)
        ]


def _nfkc_traced(value: str) -> tuple[str, list[bool], list[int]] | None:
    """Return the NFKC of `value` with, per character, whether NFKC left its
    source character unchanged and the index of that source character; None
    when NFKC of the characters one by one differs from NFKC of the whole."""
    chars: list[str] = []
    unchanged: list[bool] = []
    origin: list[int] = []
    for index, char in enumerate(value):
        mapped = unicodedata.normalize("NFKC", char)
        chars.extend(mapped)
        unchanged.extend([mapped == char] * len(mapped))
        origin.extend([index] * len(mapped))
    text = "".join(chars)
    if text != unicodedata.normalize("NFKC", value):
        return None
    return text, unchanged, origin


class _Joined:
    """The joined text of several field values, matched with exceptions.

    An exception may excuse a hit only when the hit and the exception word
    around it both lie inside the value of one field, and the exception word
    is a whole word there (`_Excuser`) with real boundaries in the joined
    text too (`_alone_in_join`: the neighbouring value's adjacent visible
    character is not a letter, digit or combining mark). Hits are found in
    two readings, the values joined by one space and the tight reading
    (`_tight_separators`), and traced back to the characters of the values;
    a hit that spans two values, or one that cannot be traced, counts. A
    hit found in either reading counts unless it is excused there.
    """

    def __init__(self, parts: Sequence[str], excuser: _Excuser) -> None:
        self._parts = tuple(parts)
        self._excuser = excuser
        # The exception spans, as (part, start, end, builtin) in the part's own
        # text; found on the first hit, so a text without hits costs nothing
        # more than the matching without exceptions.
        self._spans: list[tuple[int, int, int, bool]] | None = None
        # Per reading: its forms, where each part starts in it, and its traced
        # forms (built only when a hit there needs an exception span).
        self._readings: list[tuple[str, tuple[str, ...], list[int]]] = []
        self._traced: dict[str, list[tuple[str, list[int]]] | None] = {}
        seen: set[str] = set()
        for separators in ([" "] * len(parts), _tight_separators(parts)):
            separators[0] = ""
            text, starts = _join(parts, separators)
            if text in seen:
                continue
            seen.add(text)
            self._readings.append((text, _forms(text), starts))

    def _exception_spans(self) -> list[tuple[int, int, int, bool]]:
        if self._spans is None:
            parts = self._parts
            raw, raw_starts = _join(parts, [""] * len(parts))
            spans: list[tuple[int, int, int, bool]] = []
            for index, part in enumerate(parts):
                for start, end, builtin in self._excuser.part_runs(part):
                    offset = raw_starts[index]
                    bounds = (offset, offset + len(part))
                    if _alone_in_join(raw, offset + start, offset + end, bounds):
                        spans.append((index, start, end, builtin))
            self._spans = spans
        return self._spans

    def counts(self, word_forms: tuple[str, ...], *, builtin: bool) -> bool:
        """Return True when a hit of the word counts in either reading."""
        for text, field_forms, starts in self._readings:
            if not _forms_match(word_forms, field_forms):
                continue
            usable = [
                (starts[i] + a, starts[i] + b)
                for i, a, b, own in self._exception_spans()
                if builtin or not own
            ]
            if not usable:
                return True
            if text not in self._traced:
                self._traced[text] = _original_forms(text)
            traced = self._traced[text]
            if traced is None:
                return True
            for form, sources in traced:
                for word_form in word_forms:
                    if word_form and _hit_outside(form, sources, word_form, usable):
                        return True
        return False


def _original_forms(value: str) -> list[tuple[str, list[int]]] | None:
    """Return the three forms of `value`, each character with the index of the
    character of `value` (before NFKC) it comes from; None when not certain."""
    traced = _nfkc_traced(value)
    if traced is None:
        return None
    text, _, origin = traced
    forms = _traced_forms(text)
    if forms is None:
        return None
    return [(form, [origin[i] for i in sources]) for form, sources in forms]


def _id_name(resource_id: str) -> str:
    """Return the part of a resource id after ``:id/`` (all of it if none)."""
    _, separator, name = resource_id.partition(_ID_SEPARATOR)
    return name if separator else resource_id


def _fields(element: Element) -> tuple[tuple[str, str], ...]:
    return (
        ("text", element.text),
        ("content_desc", element.content_desc),
        ("hint", element.hint),
        ("resource_id", _id_name(element.resource_id)),
    )


class _Matcher:
    """Deny words with their forms computed once, and the exception words."""

    def __init__(self, words: Iterable[str], exceptions: Iterable[str] = ()) -> None:
        self._words = tuple(
            (word, _forms(word), _is_builtin_word(word)) for word in words
        )
        self._excuser = _Excuser(_exception_keys(exceptions))
        self._cache: dict[str, str | None] = {}
        self._plain_cache: dict[str, str | None] = {}

    @property
    def excuses(self) -> bool:
        """True when there are exception words."""
        return bool(self._excuser.keys)

    def match_joined(self, parts: Sequence[str]) -> str | None:
        """Return the first deny word that counts in the joined text of the
        field values `parts` (see `_Joined`), or None."""
        joined = _Joined(parts, self._excuser)
        return next(
            (
                word
                for word, word_forms, builtin in self._words
                if joined.counts(word_forms, builtin=builtin)
            ),
            None,
        )

    def match(self, element: Element) -> tuple[str, str] | None:
        """Return ``(word, field)`` of the first match in the element.

        The resource id is matched without the exception words.
        """
        for field, value in _fields(element):
            if value:
                word = self.match_text(value, excuse=field != "resource_id")
                if word is not None:
                    return word, field
        return None

    def match_text(self, value: str, *, excuse: bool = True) -> str | None:
        """Return the first deny word found in one string, or None.

        With `excuse` (the default), a word whose every hit lies inside an
        exception word of the string that applies to it does not count
        (`_Excuser`).
        """
        excuse = excuse and bool(self._excuser.keys)
        cache = self._cache if excuse else self._plain_cache
        if value in cache:
            return cache[value]
        field_forms = _forms(value)
        found = next(
            (
                w
                for w, word_forms, builtin in self._words
                if _forms_match(word_forms, field_forms)
                and (
                    not excuse
                    or self._excuser.counts(value, word_forms, builtin=builtin)
                )
            ),
            None,
        )
        if len(value) <= _CACHED_VALUE_LENGTH:
            cache[value] = found
        return found


_APOSTROPHES: Final = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201b": "'", "\u02bc": "'", "\u2032": "'"}
)


class _PhraseMatcher:
    """Irreversibility phrases, matched like deny words in three fields."""

    def __init__(self, phrases: Iterable[str]) -> None:
        self._matcher = _Matcher(p.translate(_APOSTROPHES) for p in phrases)

    def find(self, elements: Iterable[Element]) -> tuple[str, str, str] | None:
        """Return ``(phrase, field, element id)`` of the first match, or None."""
        for element in elements:
            for field, value in (
                ("text", element.text),
                ("content_desc", element.content_desc),
                ("hint", element.hint),
            ):
                if value:
                    phrase = self._matcher.match_text(value.translate(_APOSTROPHES))
                    if phrase is not None:
                        return phrase, field, element.id
        return None


def deny_word_in(
    element: Element, words: Iterable[str], *, exceptions: Iterable[str] = ()
) -> tuple[str, str] | None:
    """Return ``(word, field)`` of the first deny word found in the element.

    The fields are ``text``, ``content_desc``, ``hint`` and the part of
    ``resource_id`` after ``:id/``. Only the element itself is looked at.
    `exceptions` are exception words (see `word_matches`); they apply to the
    text, content description and hint, not to the resource id.

    Raises:
        TypeError: `exceptions` is one string, not a collection of words.
        ValueError: An exception word is not one word of letters and digits
            (or holds an invisible character), or equals a deny word.
    """
    words = tuple(words)
    exceptions = tuple(_check_collection(exceptions))
    matcher = _Matcher(words, exceptions)
    _check_no_conflict(exceptions, words)
    return matcher.match(element)


# -- policy ----------------------------------------------------------------------


def edge_areas(
    display: tuple[int, int],
    *,
    top: int = 24,
    bottom: int = 0,
    left: int = 0,
    right: int = 0,
) -> tuple[Bounds, ...]:
    """Build the screen-edge rectangles a press point must not fall in.

    Each size is in pixels; a size of 0 adds no rectangle.

    Raises:
        ValueError: A size is negative.
    """
    width, height = display
    if min(top, bottom, left, right) < 0:
        msg = "edge sizes must not be negative"
        raise ValueError(msg)
    areas = (
        (top, Bounds(0, 0, width, top)),
        (bottom, Bounds(0, height - bottom, width, height)),
        (left, Bounds(0, 0, left, height)),
        (right, Bounds(width - right, 0, width, height)),
    )
    return tuple(area for size, area in areas if size)


def api36_fallback_areas(display: tuple[int, int]) -> tuple[Bounds, ...]:
    """Return `edge_areas` with the sizes measured on an API 36 emulator."""
    return edge_areas(
        display,
        top=API36_TOP_PX,
        bottom=API36_BOTTOM_PX,
        left=API36_SIDE_PX,
        right=API36_SIDE_PX,
    )


@dataclass(frozen=True)
class Policy:
    """What the caller allows: the word lists, the unlabeled, number-only,
    toggle and edge rules, and whether a crawl may press BACK on a screen of
    another app.

    ``avoid`` holds the screen-edge rectangles (see `edge_areas`) that a press
    point must not fall in. The default None is the safe default: `judge` uses
    ``api36_fallback_areas(screen.display)`` for each screen. An explicit tuple,
    including ``()`` (no edge rule), is used as given.

    ``skip_number_only`` (default True) applies the ``number_only`` rule: a
    candidate whose label is only a number is not pressed. ``skip_toggles``
    (default True) applies the ``toggle`` rule: a candidate that is, or
    holds, a checkable element is not pressed. False turns the rule off.
    ``refuse_dialog_confirm`` (default True) applies the ``dialog_confirm_button``
    reason; False drops only that reason (the ``confirm_button`` reason, a deny
    word on the screen, stays).

    ``skip_irreversible`` (default True) applies the ``irreversible_screen``
    reason with the phrases of ``irreversible_phrases``; False turns it off.

    ``deny_exceptions`` (default `DEFAULT_DENY_EXCEPTIONS`, which is ``()``)
    are the exception words: a deny-word hit in a text, content description
    or hint (or in a joined text) that lies completely inside a whole word
    equal to one of them does not count (see `word_matches`), so an element
    holding the word is pressed unless something else refuses it. They
    excuse every deny word (a built-in exception word, of which there are
    none, would excuse only built-in deny words). The screen-wide search
    behind ``confirm_button`` never uses them. An exception word must not
    equal a deny word (any of its forms).

    ``back_on_other_screens`` (default False) is read by the pressing crawl
    only, not by `judge`. True lets the crawl press BACK while a screen of
    another app is in front (up to 2 in a row, then a relaunch), and that BACK
    reaches the owner of the focused window, whichever app that is. With the
    default nothing is sent to such a screen: the target app is relaunched
    (force-stop, then start), and when a 4th relaunch in a row without a sent
    tap would be needed the crawl stops.

    ``check_tap_window`` (default True) is read by the pressing crawl only.
    False lets a tap go out without reading which window takes a touch at its
    point; then a window of another app that takes touches there without
    taking the input focus (an overlay drawn over other apps, a floating
    button) gets the tap. With the default the crawl reads that window right
    before each tap and sends the tap only to a window of the app; when it
    cannot be read, nothing is sent.

    ``edges`` holds the sizes in pixels ``(top, bottom, left, right)`` that
    `judge` turns into `edge_areas` of each screen's display when ``avoid`` is
    None; None keeps the API 36 sizes. An explicit ``avoid`` wins over
    ``edges``. ``min_visible_px`` (default `MIN_VISIBLE_PX`) is the smallest
    width and height of the on-screen part of an element that `judge` counts as
    visible; it applies to the judging only, ``Screen.tap_point``, ``Screen.find`` and
    ``Screen.visible_bounds`` keep the fixed `MIN_VISIBLE_PX`.

    Raises:
        ValueError: A deny word, confirm word or irreversible phrase is empty
            after normalization, or a confirm word is empty after the label
            cleaning (invisible characters, normalization, surrounding
            punctuation), or an exception word is not one word of letters and
            digits or equals a deny word, or
            ``edges`` is not four non-negative integers, or ``min_visible_px``
            is not an integer of at least 1.
        TypeError: ``edges`` is not a sequence, ``deny_exceptions`` is one
            string, or ``back_on_other_screens`` or ``check_tap_window`` is
            not a bool.
    """

    deny_words: tuple[str, ...] = DEFAULT_DENY_WORDS
    confirm_words: tuple[str, ...] = CONFIRM_WORDS
    tap_unlabeled: bool = False
    avoid: tuple[Bounds, ...] | None = None
    skip_number_only: bool = True
    skip_toggles: bool = True
    refuse_dialog_confirm: bool = True
    edges: tuple[int, int, int, int] | None = None
    min_visible_px: int = MIN_VISIBLE_PX
    irreversible_phrases: tuple[str, ...] = IRREVERSIBLE_PHRASES
    skip_irreversible: bool = True
    back_on_other_screens: bool = False
    check_tap_window: bool = True
    deny_exceptions: tuple[str, ...] = DEFAULT_DENY_EXCEPTIONS

    def __post_init__(self) -> None:
        if type(self.back_on_other_screens) is not bool:
            msg = "back_on_other_screens must be a bool"
            raise TypeError(msg)
        if type(self.check_tap_window) is not bool:
            msg = "check_tap_window must be a bool"
            raise TypeError(msg)
        for word in (*self.deny_words, *self.confirm_words, *self.irreversible_phrases):
            _check_word(word)
        _exception_keys(self.deny_exceptions)
        _check_no_conflict(self.deny_exceptions, self.deny_words)
        if self.edges is not None and (
            len(self.edges) != _EDGE_SIDES
            or any(type(size) is not int or size < 0 for size in self.edges)
        ):
            msg = "edges must be four non-negative integers: top, bottom, left, right"
            raise ValueError(msg)
        if type(self.min_visible_px) is not int or self.min_visible_px < 1:
            msg = "min_visible_px must be an integer of at least 1"
            raise ValueError(msg)
        for word in self.confirm_words:
            if not _confirm_form(word):
                msg = f"confirm word is empty after cleaning: {word!r}"
                raise ValueError(msg)

    def with_deny_words(self, extra: Iterable[str]) -> Policy:
        """Return a copy with `extra` words (for example ``--deny``) added.

        An exception word that equals an added word is dropped from the copy,
        so the added word blocks as given.

        Raises:
            TypeError: `extra` is one string, not a collection of words.
            ValueError: A word is empty after normalization.
        """
        if isinstance(extra, str):
            msg = "extra deny words must be a collection of words, not one string"
            raise TypeError(msg)
        added = tuple(_check_word(word) for word in extra)
        merged = tuple(dict.fromkeys((*self.deny_words, *added)))
        added_forms = _deny_forms(added)
        exceptions = tuple(
            exception
            for exception in self.deny_exceptions
            if _exception_conflict(exception, added_forms) is None
        )
        return Policy(
            deny_words=merged,
            confirm_words=self.confirm_words,
            tap_unlabeled=self.tap_unlabeled,
            avoid=self.avoid,
            skip_number_only=self.skip_number_only,
            skip_toggles=self.skip_toggles,
            refuse_dialog_confirm=self.refuse_dialog_confirm,
            edges=self.edges,
            min_visible_px=self.min_visible_px,
            irreversible_phrases=self.irreversible_phrases,
            skip_irreversible=self.skip_irreversible,
            back_on_other_screens=self.back_on_other_screens,
            check_tap_window=self.check_tap_window,
            deny_exceptions=exceptions,
        )


def parse_deny_file(data: bytes) -> tuple[str, ...]:
    """Return the words of a deny file.

    The file is strict UTF-8; a leading UTF-8 BOM is accepted. Lines end at LF;
    a CR is accepted only directly before the LF (CRLF). Every raw line is
    checked before it is stripped or skipped as a comment: a C0 control
    character other than TAB, a DEL or another control character anywhere in a
    line, comment lines included, is an error. TAB counts as whitespace. Blank
    lines and lines whose first non-space character is ``#`` are skipped, and
    each word is stripped.

    Raises:
        ValueError: The data starts with a UTF-16 BOM (the message asks for a
            UTF-8 file), is not valid UTF-8, or a line holds a NUL (the message
            says it looks like UTF-16) or another control character (for
            example a bare CR line end); the message names the line.
    """
    if data.startswith(_UTF16_BOMS):
        msg = "the file is UTF-16; save it as UTF-8"
        raise ValueError(msg)
    data = data.removeprefix(_UTF8_BOM)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        msg = f"line {line} is not valid UTF-8; save the file as UTF-8"
        raise ValueError(msg) from error
    words: list[str] = []
    lines = text.split("\n")
    for number, raw in enumerate(lines, start=1):
        # Only a CR directly before an LF is a line end; the last piece has no LF.
        line_text = raw.removesuffix("\r") if number < len(lines) else raw
        if "\x00" in line_text:
            msg = f"line {number} contains NUL; it looks like UTF-16, save as UTF-8"
            raise ValueError(msg)
        for char in line_text:
            if unicodedata.category(char) == "Cc" and char != "\t":
                msg = (
                    f"line {number} contains the control character "
                    f"U+{ord(char):04X} (a bare CR line end?)"
                )
                raise ValueError(msg)
        stripped = line_text.strip()
        if stripped and not stripped.startswith("#"):
            words.append(stripped)
    return tuple(words)


# -- decisions ---------------------------------------------------------------------


@dataclass(frozen=True)
class Detail:
    """What matched for one reason.

    ``word``, ``field`` and ``element_id`` are set for ``deny_word`` (the word,
    the field and the element where it was found) and ``confirm_button`` (the
    deny word found on the screen); ``note`` is free text. A ``deny_word``
    detail may have field ``joined``: the word was found in the joined text of
    a subtree, and ``element_id`` names the subtree root. A ``confirm_button``
    detail with field ``joined`` and no ``element_id`` found the word in the
    joined text of the whole screen. When a reason comes from a hit target
    other than the candidate (``outside_target_app``, ``permission_screen``,
    ``text_input``, ``password_field``), ``element_id`` is that element; for
    the confirm reasons the hit target is named in ``note``. For ``covered``,
    ``element_id`` is the element on top of the press point. For
    ``number_only``, ``note`` and ``word`` both hold the cleaned label; for
    ``toggle``, ``element_id`` is the first checkable element in document
    order. For ``irreversible_screen``, ``word`` is the phrase, ``field`` the
    field and ``element_id`` the element where it was found.
    """

    reason: Reason
    note: str = ""
    word: str = ""
    field: str = ""
    element_id: str = ""

    def to_dict(self) -> dict[str, str]:
        """Return the JSON form: ``reason``, then ``word``, ``field``,
        ``element_id`` and ``note`` when set (the form of ``crawl --json`` and
        of ``skipped.jsonl``)."""
        optional = {
            "word": self.word,
            "field": self.field,
            "element_id": self.element_id,
            "note": self.note,
        }
        entry: dict[str, str] = {"reason": self.reason}
        entry.update({key: value for key, value in optional.items() if value})
        return entry


@dataclass(frozen=True)
class Decision:
    """The verdict on one candidate.

    ``point`` is the center of the visible part, rounded down like
    ``Screen.find``, or None without a visible part (under 8 x 8 pixels counts
    as none). ``reasons`` follow the
    order of `REASONS`; empty means the element may be pressed.
    """

    element: Element
    point: tuple[int, int] | None
    reasons: tuple[Reason, ...] = ()
    details: tuple[Detail, ...] = ()

    @property
    def pressable(self) -> bool:
        """True when no reason applies."""
        return not self.reasons


@dataclass(frozen=True)
class Judgement:
    """One `Decision` per candidate, in document order."""

    decisions: tuple[Decision, ...]

    @property
    def pressable(self) -> tuple[Decision, ...]:
        """The decisions without a reason."""
        return tuple(d for d in self.decisions if not d.reasons)

    @property
    def skipped(self) -> tuple[Decision, ...]:
        """The decisions with at least one reason."""
        return tuple(d for d in self.decisions if d.reasons)


class _Tree:
    """Index-based links of a screen; unknown parents count as roots.

    Indexes are positions in ``screen.elements``. ``pre`` lists every element in
    document (pre-)order, children in element order, so the subtree of ``i`` is
    ``pre[tin[i] : tin[i] + size[i]]``. A parent cycle is cut at its entry
    point, which then counts as a root.
    """

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        elements = screen.elements
        self.elements = elements
        count = len(elements)
        self.index = {element.id: i for i, element in enumerate(elements)}
        if len(self.index) != count:
            msg = "element ids on the screen are not unique"
            raise ValueError(msg)
        self.parent = [-1] * count
        for i, element in enumerate(elements):
            parent = self.index.get(element.parent, -1) if element.parent else -1
            self.parent[i] = -1 if parent == i else parent
        self._cut_cycles()
        self.children: list[list[int]] = [[] for _ in range(count)]
        self.roots: list[int] = []
        for i, parent in enumerate(self.parent):
            if parent == -1:
                self.roots.append(i)
            else:
                self.children[parent].append(i)
        self.pre: list[int] = []
        self.tin = [0] * count
        self.size = [1] * count
        stack = list(reversed(self.roots))
        while stack:
            current = stack.pop()
            self.tin[current] = len(self.pre)
            self.pre.append(current)
            stack.extend(reversed(self.children[current]))
        for i in reversed(self.pre):
            if self.parent[i] != -1:
                self.size[self.parent[i]] += self.size[i]

    def _cut_cycles(self) -> None:
        state = [0] * len(self.parent)
        for start in range(len(self.parent)):
            if state[start]:
                continue
            path: list[int] = []
            current = start
            while current != -1 and state[current] == 0:
                state[current] = 1
                path.append(current)
                current = self.parent[current]
            if current != -1 and state[current] == 1:
                self.parent[current] = -1
            for seen in path:
                state[seen] = 2

    def subtree(self, i: int) -> list[int]:
        """Return the element and its descendants in document order."""
        return self.pre[self.tin[i] : self.tin[i] + self.size[i]]


def _has_content(value: str) -> bool:
    return any(not char.isspace() and not _is_invisible(char) for char in value)


def _is_text_field(element: Element) -> bool:
    if _PASSWORD in element.flags:
        return True
    name = element.class_name.rsplit(".", 1)[-1].rsplit("$", 1)[-1].casefold()
    return name == _SEARCH_AUTOCOMPLETE or any(p in name for p in _TEXT_FIELD_PARTS)


def _is_dialog(tree: _Tree) -> bool:
    # The first element without a parent, and the first root of the
    # tree (an element with an unknown parent also counts): either one smaller
    # than the display makes the screen a dialog.
    width, height = tree.screen.display
    tops = [e for e in tree.elements if e.parent is None][:1]
    tops.extend(tree.elements[i] for i in tree.roots[:1])
    return any(e.bounds.width < width or e.bounds.height < height for e in tops)


def _clean(value: str) -> str:
    return _strip_invisible(value).strip()


def _add_part(spaced: list[str], tight: list[str], part: str) -> None:
    """Append a cleaned label part to both joins.

    ``spaced`` puts one space before every part but the first. ``tight`` does
    the same, except where the previous part ends and this one starts with a
    letter of the Japanese script: there it joins them directly. A half-width
    voiced sound mark is never joined directly, because NFKC would merge it
    into the letter before it.
    """
    if not spaced:
        spaced.append(part)
        tight.append(part)
        return
    previous = spaced[-1]
    spaced.extend((" ", part))
    if (
        _is_japanese_letter(previous[-1])
        and _is_japanese_letter(part[0])
        and part[0] not in _HALF_WIDTH_VOICED
    ):
        tight.extend(("", part))
    else:
        tight.extend((" ", part))


def _is_mark(char: str) -> bool:
    return char.isspace() or unicodedata.category(char)[0] in "PS"


def _strip_marks(label: str) -> str:
    """Remove whitespace, punctuation and symbols around a label.

    Both ends are trimmed until they stop changing, so ``( OK )``, ``OK!`` and
    ``[ OK ]`` all become ``OK``.
    """
    start, end = 0, len(label)
    while start < end and _is_mark(label[start]):
        start += 1
    while end > start and _is_mark(label[end - 1]):
        end -= 1
    return label[start:end]


def _is_number_only(label: str) -> bool:
    """True when the label has a numeral and nothing but numerals, whitespace,
    punctuation and symbols (categories N, Z, P and S).

    A numeral is any character of the categories Nd, Nl and No, so fractions
    (``½``) and Roman numerals (``Ⅳ``) count, as decimal digits do.
    """
    found = False
    for char in _strip_invisible(label):
        category = unicodedata.category(char)
        if category in _NUMBER_CATEGORIES:
            found = True
        elif category[0] not in "NZPS" and not char.isspace():
            return False
    return found


def _confirm_form(value: str) -> str:
    """Return the form compared with the confirm words (label and words alike)."""
    return _strip_marks(_strip_invisible(normalize(_clean(value))).strip())


# An aggregate holds, for the hit targets of one press point, the smallest
# element index that has each property (_NONE when no hit target has it).
_PKG, _PERM, _TEXT, _PASSWORD_AT, _CONFIRM, _DENY, _JOINED = range(7)
_NONE: Final = 1 << 30
_Agg = tuple[int, int, int, int, int, int, int]
_EMPTY_AGG: Final[_Agg] = (_NONE, _NONE, _NONE, _NONE, _NONE, _NONE, _NONE)
_CELLS_X: Final = 24
_CELLS_Y: Final = 48


def _merge(first: _Agg, second: _Agg) -> _Agg:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
        min(first[4], second[4]),
        min(first[5], second[5]),
        min(first[6], second[6]),
    )


class _Grid:
    """A grid over the display that finds the rectangles holding a point.

    A rectangle is listed in every cell it touches, as ``full`` when it covers
    the cell completely (nothing to test per point) or ``partial``. A
    rectangle covering the whole display is listed once, in ``everywhere``.
    """

    def __init__(self, display: tuple[int, int], rects: list[tuple[int, Bounds]]):
        width, height = display
        self.cell_w = max(1, -(-width // _CELLS_X))
        self.cell_h = max(1, -(-height // _CELLS_Y))
        self.cols = -(-width // self.cell_w) if width > 0 else 1
        self.everywhere: list[int] = []
        self.full: dict[int, list[int]] = {}
        self.partial: dict[int, list[tuple[int, Bounds]]] = {}
        for i, rect in rects:
            if (
                rect.left <= 0
                and rect.top <= 0
                and rect.right >= width
                and rect.bottom >= height
            ):
                self.everywhere.append(i)
                continue
            self._add(i, rect, width, height)

    def _add(self, i: int, rect: Bounds, width: int, height: int) -> None:
        cell_w, cell_h = self.cell_w, self.cell_h
        for row in range(rect.top // cell_h, (rect.bottom - 1) // cell_h + 1):
            top = row * cell_h
            bottom = min(top + cell_h, height)
            tall = rect.top <= top and rect.bottom >= bottom
            for col in range(rect.left // cell_w, (rect.right - 1) // cell_w + 1):
                left = col * cell_w
                right = min(left + cell_w, width)
                key = row * self.cols + col
                if tall and rect.left <= left and rect.right >= right:
                    self.full.setdefault(key, []).append(i)
                else:
                    self.partial.setdefault(key, []).append((i, rect))

    def cell(self, point: tuple[int, int]) -> int:
        """Return the key of the cell holding the point."""
        return (point[1] // self.cell_h) * self.cols + point[0] // self.cell_w


class _Judge:
    """State shared by the decisions of one screen."""

    def __init__(
        self,
        screen: Screen,
        target_package: str,
        policy: Policy,
        foreground: str | None,
    ) -> None:
        self.screen = screen
        self.policy = policy
        self.tree = _Tree(screen)
        self.elements = screen.elements
        self.matcher = _Matcher(policy.deny_words, policy.deny_exceptions)
        self.confirm = frozenset(_confirm_form(w) for w in policy.confirm_words)
        # The lengths of the confirm words, shortest first: only a start of a
        # label of one of these lengths can be a confirm word.
        self._confirm_lengths = sorted({len(form) for form in self.confirm})
        # A confirm word that ends in a letter of the Japanese script can be
        # followed by anything; the others need a boundary (`_is_confirm`).
        self._open_ended = frozenset(
            form for form in self.confirm if _is_japanese_letter(form[-1])
        )
        self.target = target_package
        self._confirm_forms: dict[str, str] = {}
        self.visible = [self._visible_part(e) for e in self.elements]
        self.clickable = [_CLICKABLE in e.flags for e in self.elements]
        self.meaningful = [e.is_meaningful for e in self.elements]
        self._tops: dict[tuple[int, int], int] = {}
        self._cell_tops: dict[int, int] = {}
        self._avoid = self._edge_rectangles()
        self.screen_note = ""
        if foreground is None:
            self.screen_note = "foreground app unknown"
        elif foreground != target_package:
            self.screen_note = f"foreground is {foreground}"
        elif screen.package != target_package:
            self.screen_note = f"screen package is {screen.package}"
        self.dialog = _is_dialog(self.tree)
        self._screen_hit: Detail | Literal[False] | None = False
        self._irreversible: Detail | Literal[False] | None = False
        self._summarize()
        self._joined_hit: dict[int, str | None] = {}
        # Per clickable root: the first clickable element below it whose own
        # joined text holds a deny word (only used with exception words).
        self._joined_below: dict[int, int] = {}
        self._labels: dict[int, str] = {}
        self._contrib: dict[int, _Agg] = {}
        self._everywhere: _Agg | None = None
        self._cells: dict[int, _Agg] = {}
        self._points: dict[tuple[int, int], _Agg] = {}
        # The hit targets keep slivers: a part under 8 pixels is not a visible
        # element (see `visible`) but a press that lands on it still reaches it.
        parts = (screen._clip(e) for e in self.elements)  # noqa: SLF001
        rects = [(i, rect) for i, rect in enumerate(parts) if rect is not None]
        self.grid = _Grid(screen.display, rects)

    def _visible_part(self, element: Element) -> Bounds | None:
        """Return the part of `element` on the display, or None for a sliver."""
        clipped = self.screen._clip(element)  # noqa: SLF001
        if clipped is None:
            return None
        if min(clipped.width, clipped.height) < self.policy.min_visible_px:
            return None
        return clipped

    def _edge_rectangles(self) -> tuple[Bounds, ...]:
        policy = self.policy
        if policy.avoid is not None:
            return policy.avoid
        if policy.edges is None:
            return api36_fallback_areas(self.screen.display)
        top, bottom, left, right = policy.edges
        return edge_areas(
            self.screen.display, top=top, bottom=bottom, left=left, right=right
        )

    def _summarize(self) -> None:
        """Compute, in a few linear passes over the tree, what the decisions
        need to know about every element and its subtree.
        """
        count = len(self.elements)
        # One pass over the tree, children before parents: the first deny
        # hit in an element's own fields and in its subtree, whether the
        # subtree holds a password flag, and whether it has a visible label.
        self.own_hit = [self.matcher.match(e) for e in self.elements]
        self.sub_deny = [i if self.own_hit[i] else _NONE for i in range(count)]
        self.sub_password = [_PASSWORD in e.flags for e in self.elements]
        self.sub_labeled = [self._is_labeled(e) for e in self.elements]
        # The document position of the first checkable element of the subtree.
        self.sub_checkable = [
            self.tree.tin[i] if _CHECKABLE in e.flags else _NONE
            for i, e in enumerate(self.elements)
        ]
        # The first text or content_desc with content among the descendants of
        # each element in document order (None when there is none).
        self.below_label: list[str | None] = [None] * count
        # The document position of the first element of the subtree with a
        # confirm word in one of its own fields, and how many elements of the
        # subtree have a text (a content_desc) with a real character.
        self.sub_confirm = [
            self.tree.tin[i] if self._own_confirm(e) else _NONE
            for i, e in enumerate(self.elements)
        ]
        self.core_text = [int(self._is_core(e.text)) for e in self.elements]
        self.core_desc = [int(self._is_core(e.content_desc)) for e in self.elements]
        self.sub_core_text = list(self.core_text)
        self.sub_core_desc = list(self.core_desc)
        parent = self.tree.parent
        for i in reversed(self.tree.pre):
            up = parent[i]
            if up != -1:
                self.sub_deny[up] = min(self.sub_deny[up], self.sub_deny[i])
                self.sub_password[up] |= self.sub_password[i]
                self.sub_labeled[up] |= self.sub_labeled[i]
                self.sub_checkable[up] = min(
                    self.sub_checkable[up], self.sub_checkable[i]
                )
                first = self._own_label(self.elements[i]) or self.below_label[i]
                if first is not None:
                    self.below_label[up] = first
                self.sub_confirm[up] = min(self.sub_confirm[up], self.sub_confirm[i])
                self.sub_core_text[up] += self.sub_core_text[i]
                self.sub_core_desc[up] += self.sub_core_desc[i]
        # The smallest index of an own-field hit in the subtrees of the
        # clickable ancestors of each element.
        self.above_deny = [_NONE] * count
        for i in self.tree.pre:
            up = parent[i]
            if up != -1:
                self.above_deny[i] = self.above_deny[up]
                if self.clickable[up]:
                    self.above_deny[i] = min(self.above_deny[i], self.sub_deny[up])
        # The highest clickable element at or above each element. Without
        # exception words, a deny word in the joined text of a clickable
        # subtree is also in the joined text of every clickable ancestor, so
        # only this one is matched (see `joined_root` for exception words).
        self.top = [-1] * count
        for i in self.tree.pre:
            up = parent[i]
            inherited = self.top[up] if up != -1 else -1
            self.top[i] = (
                inherited if inherited != -1 else (i if self.clickable[i] else -1)
            )

    @staticmethod
    def _is_labeled(element: Element) -> bool:
        return any(
            _has_content(value)
            for value in (element.text, element.content_desc, element.hint)
        )

    @staticmethod
    def _own_label(element: Element) -> str | None:
        """Return the text, else the content_desc, that has content, or None."""
        for value in (element.text, element.content_desc):
            if _has_content(value):
                return value
        return None

    def _number_label(self, i: int) -> str:
        """Return the label that the ``number_only`` rule reads, or ``""``.

        The candidate's own text, else its content_desc, else its hint, else
        the first text or content_desc with content among its descendants in
        document order.
        """
        element = self.elements[i]
        for value in (element.text, element.content_desc, element.hint):
            if _has_content(value):
                return value
        return self.below_label[i] or ""

    def _is_confirm(self, form: str) -> bool:
        """True when the form is a confirm word or starts with one.

        A confirm word at the start counts when a boundary follows it: a
        character that is not a letter, or nothing. A word ending in a
        letter of the Japanese script needs no boundary.
        """
        if not form:
            return False
        if form in self.confirm:
            return True
        for end in self._confirm_lengths:
            if end >= len(form):
                break
            head = form[:end]
            if head in self.confirm and (
                head in self._open_ended or not form[end].isalpha()
            ):
                return True
        return False

    def _form(self, value: str) -> str:
        form = self._confirm_forms.get(value)
        if form is None:
            form = self._confirm_forms[value] = _confirm_form(value)
        return form

    # -- screen-wide deny words ----------------------------------------------------

    def screen_deny(self) -> Detail | None:
        """Return the first deny word match anywhere on the screen.

        Each field of each element is matched alone, in element order. When
        none matches, the texts, the content_descs and the text and
        content_desc sequence of the whole screen in document order, each
        joined by one space, are matched (field ``joined``, no element).
        The exception words are not used here: a deny word anywhere on the
        screen counts, even inside an exception word.
        """
        if self._screen_hit is False:
            self._screen_hit = None
            plain = self._plain_matcher()
            hits = (
                self.own_hit
                if plain is self.matcher
                else [plain.match(e) for e in self.elements]
            )
            for i, hit in enumerate(hits):
                if hit:
                    self._screen_hit = Detail(
                        "confirm_button",
                        word=hit[0],
                        field=hit[1],
                        element_id=self.elements[i].id,
                    )
                    break
            else:
                word = self._joined_word(
                    [self.elements[i] for i in self.tree.pre], plain
                )
                if word is not None:
                    self._screen_hit = Detail(
                        "confirm_button", word=word, field="joined"
                    )
        return self._screen_hit

    def _plain_matcher(self) -> _Matcher:
        """Return the deny words without exceptions (the matcher itself when
        the policy has no exception words).
        """
        if not self.policy.deny_exceptions:
            return self.matcher
        return _Matcher(self.policy.deny_words)

    def _joined_word(
        self, elements: Iterable[Element], matcher: _Matcher | None = None
    ) -> str | None:
        """Return the deny word found in the joined text of the elements.

        Three strings are matched, each joined by one space: the text and
        content_desc sequence, the texts alone and the content_descs alone, so
        that a field between two parts of a phrase does not hide it.
        """
        parts: list[str] = []
        texts: list[str] = []
        descs: list[str] = []
        for element in elements:
            if element.text:
                parts.append(element.text)
                texts.append(element.text)
            if element.content_desc:
                parts.append(element.content_desc)
                descs.append(element.content_desc)
        matcher = matcher or self.matcher
        for joined in (parts, texts, descs):
            if not matcher.excuses:
                word = matcher.match_text(" ".join(joined))
            else:
                word = matcher.match_joined(joined) if joined else None
            if word is not None:
                return word
        return None

    def joined_hit(self, root: int) -> str | None:
        """Return the deny word found in the joined text of a subtree, if any."""
        if root not in self._joined_hit:
            tree = self.tree
            self._joined_hit[root] = self._joined_word(
                self.elements[i] for i in tree.subtree(root)
            )
        return self._joined_hit[root]

    def joined_root(self, i: int) -> int:
        """Return the clickable root whose joined subtree text holds a deny
        word and whose subtree holds the element, or ``_NONE``.

        The scope is the subtree of the highest clickable element at or above
        it. Without exception words its joined text is enough: a deny word in
        the joined text of a subtree is also in that of every clickable
        ancestor. With exception words that does not hold (a longer exception
        word can form in the ancestor's text), so the joined text of every
        clickable element in that subtree is matched as well, the root first,
        then in document order; the first one with a hit is returned.
        """
        root = self.top[i]
        if root == -1:
            return _NONE
        if self.joined_hit(root) is not None:
            return root
        if not self.matcher.excuses:
            return _NONE
        if root not in self._joined_below:
            self._joined_below[root] = next(
                (
                    j
                    for j in self.tree.subtree(root)[1:]
                    if self.clickable[j] and self.joined_hit(j) is not None
                ),
                _NONE,
            )
        return self._joined_below[root]

    # -- confirm labels ------------------------------------------------------------

    def confirm_label(self, i: int) -> str:
        """Return the confirm form that labels a clickable element, or ``""``.

        The sources are its own text, content_desc and hint, the same three
        fields of each descendant, and the descendants' texts (and likewise
        content_descs) joined in document order (`_joined_forms`).
        """
        cached = self._labels.get(i)
        if cached is None:
            cached = self._labels[i] = self._find_label(i)
        return cached

    def _find_label(self, i: int) -> str:
        position = self.sub_confirm[i]
        if position != _NONE:
            element = self.elements[self.tree.pre[position]]
            for value in (element.text, element.content_desc, element.hint):
                form = self._form(value)
                if self._is_confirm(form):
                    return form
        below = self.tree.subtree(i)[1:]
        # A single part is a field that was matched above.
        for getter, count in (
            (lambda e: e.text, self.sub_core_text[i] - self.core_text[i]),
            (lambda e: e.content_desc, self.sub_core_desc[i] - self.core_desc[i]),
        ):
            if count < _MIN_JOINED_PARTS:
                continue
            values = (getter(self.elements[j]) for j in below)
            for form in self._joined_forms(values):
                if self._is_confirm(form):
                    return form
        return ""

    def _joined_forms(self, values: Iterable[str]) -> tuple[str, str]:
        """Return the confirm forms of the values joined in document order.

        The first form joins the cleaned values with one space. The second
        joins them with no space where one value ends and the next starts
        with a letter of the Japanese script (``確`` + ``認してください``),
        which is written without spaces, and with one space elsewhere.
        """
        spaced: list[str] = []
        tight: list[str] = []
        for value in values:
            part = _clean(value)
            if part:
                _add_part(spaced, tight, part)
        return _confirm_form("".join(spaced)), _confirm_form("".join(tight))

    def _own_confirm(self, element: Element) -> bool:
        return any(
            self._is_confirm(form)
            for form in map(
                self._form, (element.text, element.content_desc, element.hint)
            )
        )

    def _is_core(self, value: str) -> bool:
        """True when the value has a character that is not a mark or a space."""
        form = _strip_invisible(normalize(_clean(value)))
        return any(not _is_mark(char) for char in form)

    # -- hit targets ---------------------------------------------------------------

    def contribution(self, i: int) -> _Agg:
        """Return what the element adds to a point it holds."""
        cached = self._contrib.get(i)
        if cached is None:
            element = self.elements[i]
            clickable = self.clickable[i]
            deny = min(self.above_deny[i], i if self.own_hit[i] else _NONE)
            if clickable:
                deny = min(deny, self.sub_deny[i])
            cached = self._contrib[i] = (
                i if element.package != self.target else _NONE,
                i if element.package in PERMISSION_PACKAGES else _NONE,
                i if _is_text_field(element) else _NONE,
                i
                if _PASSWORD in element.flags or (clickable and self.sub_password[i])
                else _NONE,
                i if clickable and self.confirm_label(i) else _NONE,
                deny,
                self.joined_root(i),
            )
        return cached

    def _fold(self, indexes: Iterable[int]) -> _Agg:
        result = _EMPTY_AGG
        for i in indexes:
            result = _merge(result, self.contribution(i))
        return result

    def point_aggregate(self, point: tuple[int, int]) -> _Agg:
        """Return the aggregate over every element whose visible part holds the
        point (the hit targets).
        """
        cached = self._points.get(point)
        if cached is None:
            grid = self.grid
            if self._everywhere is None:
                self._everywhere = self._fold(grid.everywhere)
            key = grid.cell(point)
            cell = self._cells.get(key)
            if cell is None:
                cell = self._cells[key] = self._fold(grid.full.get(key, ()))
            x, y = point
            cached = _merge(self._everywhere, cell)
            cached = _merge(
                cached,
                self._fold(
                    i
                    for i, rect in grid.partial.get(key, ())
                    if rect.left <= x < rect.right and rect.top <= y < rect.bottom
                ),
            )
            self._points[point] = cached
        return cached

    def topmost(self, point: tuple[int, int]) -> int:
        """Return the index of the last meaningful element whose visible part
        holds the point (it is drawn on top), or ``-1`` when there is none.
        This is ``Screen.hit_target`` on the grid. The grid also holds the
        slivers (see `__init__`); they count here too, as in ``hit_target``,
        which uses the part on the display with no size threshold.

        A candidate is always clickable, so it is meaningful, and its visible
        part holds its own press point: the result is never ``-1`` for the
        point of a candidate (the ``Screen.tap_point`` refusal ``no_target``
        cannot happen here).
        """
        cached = self._tops.get(point)
        if cached is None:
            grid = self.grid
            key = grid.cell(point)
            base = self._cell_tops.get(key)
            if base is None:
                # The highest meaningful element covering the whole cell (or
                # the whole display): no point of the cell can be below it.
                base = -1
                for i in (*grid.everywhere, *grid.full.get(key, ())):
                    if self.meaningful[i] and i > base:
                        base = i
                self._cell_tops[key] = base
            x, y = point
            top = base
            # The partial rectangles are in document order: from the last one
            # down, the first hit is the answer, and one at or below `base`
            # cannot beat it.
            for i, rect in reversed(grid.partial.get(key, ())):
                if i <= base:
                    break
                if (
                    self.meaningful[i]
                    and rect.left <= x < rect.right
                    and rect.top <= y < rect.bottom
                ):
                    top = i
                    break
            cached = self._tops[point] = top
        return cached

    # -- one candidate -------------------------------------------------------------

    def _hit(self, own: _Agg, found: _Agg, kind: int, i: int) -> tuple[int, bool]:
        """Return the element that causes a reason and whether it is another
        element than the candidate (the candidate itself is named first).
        """
        if own[kind] != _NONE:
            return i, False
        return found[kind], True

    def _cover(self, i: int, point: tuple[int, int] | None) -> Detail | None:
        """Return the ``covered`` detail when another element is on top of the
        press point: the topmost meaningful element there is neither the
        candidate nor one of its descendants.
        """
        if point is None:
            return None
        top = self.topmost(point)
        tree = self.tree
        if top in (-1, i) or tree.tin[i] <= tree.tin[top] < tree.tin[i] + tree.size[i]:
            return None
        ident = self.elements[top].id
        return Detail("covered", note=f"covered by {ident}", element_id=ident)

    def decide(self, i: int) -> Decision:
        """Judge one candidate."""
        element = self.elements[i]
        visible = self.visible[i]
        point = None if visible is None else visible.center
        own = self.contribution(i)
        found = own if point is None else _merge(own, self.point_aggregate(point))
        reasons: list[Reason] = []
        details: list[Detail] = []

        def other(reason: Reason, kind: int) -> None:
            hit, elsewhere = self._hit(own, found, kind, i)
            if elsewhere:
                ident = self.elements[hit].id
                details.append(Detail(reason, note="hit target", element_id=ident))

        if self.screen_note:
            reasons.append("outside_target_app")
            details.append(Detail("outside_target_app", note=self.screen_note))
        elif found[_PKG] != _NONE:
            reasons.append("outside_target_app")
            hit, elsewhere = self._hit(own, found, _PKG, i)
            note = f"element package is {self.elements[hit].package!r}"
            ident = self.elements[hit].id if elsewhere else ""
            details.append(Detail("outside_target_app", note=note, element_id=ident))
        if found[_PERM] != _NONE:
            reasons.append("permission_screen")
            other("permission_screen", _PERM)
        if visible is None:
            reasons.append("not_visible")
        elif (cover := self._cover(i, point)) is not None:
            reasons.append("covered")
            details.append(cover)
        if _DISABLED in element.flags:
            reasons.append("disabled")
        if found[_TEXT] != _NONE:
            reasons.append("text_input")
            other("text_input", _TEXT)
        if found[_PASSWORD_AT] != _NONE:
            reasons.append("password_field")
            other("password_field", _PASSWORD_AT)
        if not self.policy.tap_unlabeled and not self.sub_labeled[i]:
            reasons.append("unlabeled")
        self._crawl_rules(i, reasons, details)
        self._word_rules(found, reasons, details)
        if found[_CONFIRM] != _NONE:
            self._confirm_reasons(i, own, found, reasons, details)
        if point is not None and any(_contains(area, point) for area in self._avoid):
            reasons.append("screen_edge")
        return Decision(element, point, tuple(reasons), tuple(details))

    def _crawl_rules(
        self, i: int, reasons: list[Reason], details: list[Detail]
    ) -> None:
        """Add the ``number_only`` and ``toggle`` reasons, each its own check."""
        if self.policy.skip_number_only:
            label = self._number_label(i)
            if _is_number_only(label):
                reasons.append("number_only")
                note = _clean(label)
                details.append(Detail("number_only", note=note, word=note))
        if self.policy.skip_toggles and self.sub_checkable[i] != _NONE:
            reasons.append("toggle")
            checkable = self.elements[self.tree.pre[self.sub_checkable[i]]]
            details.append(Detail("toggle", element_id=checkable.id))

    def _word_rules(
        self, found: _Agg, reasons: list[Reason], details: list[Detail]
    ) -> None:
        """Add the ``deny_word`` and ``irreversible_screen`` reasons."""
        deny = self._deny_detail(found)
        if deny:
            reasons.append("deny_word")
            details.append(deny)
        irreversible = self.irreversible_detail()
        if irreversible is not None:
            reasons.append("irreversible_screen")
            details.append(irreversible)

    def irreversible_detail(self) -> Detail | None:
        """Return the irreversibility phrase found on the screen, if any.

        Computed once per screen; always None when the rule is off.
        """
        if self._irreversible is False:
            self._irreversible = None
            if self.policy.skip_irreversible and self.policy.irreversible_phrases:
                hit = _PhraseMatcher(self.policy.irreversible_phrases).find(
                    self.elements
                )
                if hit is not None:
                    phrase, field, ident = hit
                    self._irreversible = Detail(
                        "irreversible_screen",
                        word=phrase,
                        field=field,
                        element_id=ident,
                    )
        return self._irreversible

    def _deny_detail(self, found: _Agg) -> Detail | None:
        """Return the deny word of the scope: own fields first, then joined."""
        if found[_DENY] != _NONE:
            hit = self.own_hit[found[_DENY]]
            if hit:
                return Detail(
                    "deny_word",
                    word=hit[0],
                    field=hit[1],
                    element_id=self.elements[found[_DENY]].id,
                )
        if found[_JOINED] != _NONE:
            root = found[_JOINED]
            word = self.joined_hit(root)
            if word is not None:
                return Detail(
                    "deny_word",
                    word=word,
                    field="joined",
                    element_id=self.elements[root].id,
                )
        return None

    def _confirm_reasons(
        self,
        i: int,
        own: _Agg,
        found: _Agg,
        reasons: list[Reason],
        details: list[Detail],
    ) -> None:
        hit, elsewhere = self._hit(own, found, _CONFIRM, i)
        label = self.confirm_label(hit)
        note = f"{label} (hit target {self.elements[hit].id})" if elsewhere else label
        screen_deny = self.screen_deny()
        if screen_deny:
            reasons.append("confirm_button")
            details.append(
                Detail(
                    reason=screen_deny.reason,
                    note=note,
                    word=screen_deny.word,
                    field=screen_deny.field,
                    element_id=screen_deny.element_id,
                )
            )
        if self.dialog and self.policy.refuse_dialog_confirm:
            reasons.append("dialog_confirm_button")
            details.append(Detail("dialog_confirm_button", note=note))


def _contains(bounds: Bounds, point: tuple[int, int]) -> bool:
    x, y = point
    return bounds.left <= x < bounds.right and bounds.top <= y < bounds.bottom


def judge(
    screen: Screen,
    target_package: str,
    policy: Policy,
    *,
    foreground: str | None,
) -> Judgement:
    """Decide for every candidate of the screen whether it may be pressed.

    Candidates are the elements with the ``clickable`` flag; other elements
    are not listed. When `foreground` is None (unknown), differs from
    `target_package`, or the screen's package does, every candidate gets
    ``outside_target_app``, except on a screen with more than `MAX_ELEMENTS`
    elements, where every candidate gets only ``screen_too_large``.

    The hit targets of a candidate are all elements whose visible part holds
    its press point: the candidate itself, its ancestors and every overlay.
    Several reasons look at the hit targets, not only at the candidate, so a
    button under a password field, a permission button or a confirm button is
    refused at the same point where the element on top is. The detail of such a
    reason names the hit target (``element_id``, or the id in ``note``).

    Reasons, in this order, each listed once when it applies:

    * ``screen_too_large``: the screen has more than `MAX_ELEMENTS` elements.
      Every candidate gets only this reason, no point and no other work is
      done (the cost of the other rules grows with the number of elements).
    * ``outside_target_app``: foreground unknown or not the target, or the
      screen is in another package, or any hit target is.
    * ``permission_screen``: any hit target belongs to a permission package.
    * ``not_visible``: no part of the element is on the display, or the part
      is under 8 pixels wide or under 8 pixels high.
    * ``covered``: the topmost meaningful element (clickable, or with a
      non-empty text, content description or hint; the last in document order)
      whose part on the display holds the press point (any size: a sliver
      on top counts) is neither the candidate nor one of its descendants.
      The detail's ``element_id`` is that element.
    * ``disabled``: the element has the ``disabled`` flag.
    * ``text_input``: any hit target is a text field (password flag or the
      class rule); pressing it raises the keyboard.
    * ``password_field``: any hit target has the password flag, or the
      candidate or a clickable hit target has a descendant with it. A
      non-clickable container holding the point does not count its
      descendants, or one password field would block the whole screen.
    * ``unlabeled``: neither the element nor a descendant has a text,
      content description or hint (unless ``tap_unlabeled``).
    * ``number_only``: the label (own text, else content description, else
      hint, else the first non-empty text or content description of a
      descendant in document order, invisible characters removed) has a
      numeral (a digit, a fraction or a Roman numeral) and otherwise only
      whitespace, punctuation and symbols (unless
      ``skip_number_only`` is False). The detail's ``note`` and ``word`` hold
      the label.
    * ``toggle``: the element or a descendant has the ``checkable`` flag
      (unless ``skip_toggles`` is False). The detail's ``element_id`` is the
      first checkable element in document order. A row that holds a switch is
      refused even when pressing the row opens a sub-screen.
    * ``deny_word``: a deny word is found in the deny scope: the element and
      its descendants, the whole subtree of every clickable ancestor
      (nested rows and cards are all in scope), and, for every hit target, its
      own fields, its whole subtree when it is clickable, and the subtrees of
      its clickable ancestors. Each field is matched alone, then the joined
      subtree text (field ``joined``) of the highest clickable element in
      scope above the candidate and above each clickable hit target: the
      text and content_desc sequence, the texts alone and the content_descs
      alone. With exception words, the joined text of every clickable
      element in that highest subtree is matched as well, siblings of the
      candidate included, and the detail's ``element_id`` names the clickable
      element whose joined text held the hit. A hit inside an exception word
      of ``policy.deny_exceptions`` that stands whole in a text, content_desc
      or hint, or in one field's value inside a joined text with real
      boundaries around it, does not count (see the module docstring); the
      resource id has no exceptions.
    * ``irreversible_screen``: the text, content description or hint of any
      element of the screen holds a phrase of ``policy.irreversible_phrases``
      (unless ``skip_irreversible`` is False). Every candidate gets it. The
      detail's ``word`` is the phrase, ``field`` the field and ``element_id``
      the first element (in document order) where it was found.
    * ``confirm_button``: a confirm word is the label of the candidate or of
      any clickable hit target (any source: own text, content_desc, hint,
      those of any descendant, or the joined descendant texts or
      content_descs) and a deny word is found anywhere on the screen, alone in
      a field or in the joined texts, content_descs or text and content_desc
      sequence of the whole screen (without the exception words).
    * ``dialog_confirm_button``: a confirm word is the label (same sources) and
      the screen is
      a dialog (its first top-level element is smaller than the display; the first
      element with an unknown parent counts as top-level too).
    * ``screen_edge``: the press point lies in a ``policy.avoid`` rectangle
      (None means ``api36_fallback_areas(screen.display)``; ``()`` turns it off).

    Raises:
        ValueError: Two elements of the screen have the same id.
    """
    elements = screen.elements
    if len(elements) > MAX_ELEMENTS:
        if len({element.id for element in elements}) != len(elements):
            msg = "element ids on the screen are not unique"
            raise ValueError(msg)
        note = f"{len(elements)} elements; the limit is {MAX_ELEMENTS}"
        verdict = (Detail("screen_too_large", note=note),)
        return Judgement(
            tuple(
                Decision(element, None, ("screen_too_large",), verdict)
                for element in elements
                if _CLICKABLE in element.flags
            )
        )
    state = _Judge(screen, target_package, policy, foreground)
    return Judgement(
        tuple(
            state.decide(i) for i, element in enumerate(elements) if state.clickable[i]
        )
    )
