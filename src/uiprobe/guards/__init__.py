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

The default deny words come from five lists: the base list (purchases,
payments, sending, deleting, signing out and similar), a list of further
actions such as subscriptions and starting over, the nine words for
disabling, force-stopping and signing in (``_SETTINGS_DENY_WORDS``), and the
four words for carts and accounts (``_SHOP_DENY_WORDS``), and the sixteen words
for state-changing verbs of Settings pages such as "Forget" and "Disconnect"
(``_STATE_DENY_WORDS``, added in defaults version 7).

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

Six crawl rules, each a separate check that a
`Policy` field turns on or off:

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
* ``two_target_row`` (``Policy.skip_two_target_rows``, on by default): the
  candidate's subtree (its descendants) holds an element whose resource id
  ends with ``:id/two_target_divider``. AOSP Settings draws that divider
  between the main area of a two-target row and its second button: on the
  Internet screen, pressing the main area of the "T-Mobile" row switches the
  active network, and the gear next to it opens the details. Only the row is
  refused; the elements inside it (the gear) are judged on their own.
* ``row_with_button`` (``Policy.skip_rows_with_buttons``, off by default):
  the candidate has a clickable descendant. It catches two-target rows that
  have no divider id, which is expected on vendor Settings apps such as
  Samsung's One UI (not confirmed: only its home page was recorded). It also
  refuses list rows of ordinary apps that hold their own button (a mail row
  with a star, a song row with a menu). When both match, only
  ``two_target_row`` is given.

* ``navigate_up`` (``Policy.skip_navigate_up``, on by default): the
  candidate is the up button of a toolbar. A crawl takes the first pressable
  element of a screen, and on a sub-page that is the up button at the top
  left: pressing it at once returns to the parent screen, so the rows of the
  sub-page are never pressed (the crawl goes back up with its own BACK
  instead). The button is found by its structure first: an element whose
  class ends with ``ImageButton`` or ``ImageView``, that is the first
  clickable child of its parent, where the parent's class ends with
  ``Toolbar`` (``android.widget.Toolbar``, ``MaterialToolbar``, ...) or the
  parent's resource id ends with ``:id/action_bar`` (the dump reports a
  Toolbar as ``android.view.ViewGroup``; AOSP Settings gives it that id); or
  an element whose resource id ends with ``:id/home`` or ``:id/up`` (the up
  button of the older action bar). It is also refused when its content
  description, or its label (own text, else content description, else hint,
  else the first text or content description of a descendant, as for
  ``number_only``), is exactly ``Navigate up``: that is app text, but an app
  that forges it only makes uiprobe refuse more. Known over-block: the first
  clickable image button of a toolbar on a root screen (a drawer button) is
  not pressed either.
* ``slider`` (``Policy.skip_sliders``, on by default): the candidate or an
  element in its subtree is an adjustable control: its class, after the last
  ``.`` and ``$``, ends with ``SeekBar`` (``AbsSeekBar``,
  ``AppCompatSeekBar``), ``RatingBar`` or ``Slider`` (the Material
  ``Slider`` and ``RangeSlider``). A tap on such a control moves it to the
  tapped place and so changes a value: a Settings row "Notification
  vibration" whose centre lay on its SeekBar changed the vibration intensity
  from 5 to 2, and as the slider's description changed, the row was pressed
  again. The press point of a candidate is always the centre of its visible
  part, so a candidate whose press point lies inside the part on the display
  of any slider on the screen (whatever its size) is refused as well, even
  when the slider is not in its subtree. How a Jetpack Compose slider is
  reported in a dump has not been checked; the class rule finds it only if
  it is reported with one of these class names. A ``ProgressBar`` is not
  refused (it shows a value and takes no input).

Neither row rule sees a row with no structural sign: a saved Wi-Fi network
row whose press reconnects, with no divider and no button inside, is still
pressed.

These rules only add refusals. In `REASONS` they come right after
``unlabeled``: ``number_only``, ``toggle``, ``two_target_row``,
``row_with_button``, ``navigate_up``, then ``slider``.

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
``toggle``, ``two_target_row``, ``row_with_button``, ``navigate_up``,
``slider``, ``deny_word``, ``irreversible_screen``, ``confirm_button``,
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

import re  # noqa: F401
import unicodedata  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from typing import TYPE_CHECKING, Final, Literal, get_args  # noqa: F401

from uiprobe.guards.constants import (  # noqa: F401
    _ACTION_BAR_ID,
    _APOSTROPHES,
    _BASE_DENY_WORDS,
    _CACHED_VALUE_LENGTH,
    _CHECKABLE,
    _CLICKABLE,
    _DEFAULT_IGNORABLE,
    _DISABLED,
    _EDGE_SIDES,
    _EMPTY_CATEGORIES,
    _EXTRA_DENY_WORDS,
    _HALF_WIDTH_VOICED,
    _ID_SEPARATOR,
    _JAPANESE_RANGES,
    _MIN_JOINED_PARTS,
    _NAVIGATE_UP,
    _NUMBER_CATEGORIES,
    _PASSWORD,
    _SEARCH_AUTOCOMPLETE,
    _SEPARATOR_CHARS,
    _SETTINGS_DENY_WORDS,
    _SHOP_DENY_WORDS,
    _SLIDER_CLASSES,
    _SPACES_RE,
    _STATE_DENY_WORDS,
    _TEXT_FIELD_PARTS,
    _TOOLBAR_CLASS,
    _TWO_TARGET_DIVIDER,
    _UP_CLASSES,
    _UP_IDS,
    _UTF8_BOM,
    _UTF16_BOMS,
    _WORD_REASONS,
    API36_BOTTOM_PX,
    API36_SIDE_PX,
    API36_TOP_PX,
    CONFIRM_WORDS,
    DEFAULT_DENY_EXCEPTIONS,
    DEFAULT_DENY_WORDS,
    DEFAULT_SCROLL_AMOUNT,
    DEFAULT_SCROLL_INCIDENTS,
    DEFAULT_SCROLL_LISTS,
    DEFAULT_SCROLLS_PER_SCREEN,
    IRREVERSIBLE_PHRASES,
    MAX_ELEMENTS,
    MAX_SCROLL_AMOUNT,
    PERMISSION_PACKAGES,
    REASONS,
    SCROLL_CONTAINER_CLASSES,
    SCROLL_POINT_CLASSES,
    Reason,
)
from uiprobe.guards.judge import (  # noqa: F401
    _contains,
    _Judge,
    judge,
)
from uiprobe.guards.matching import (  # noqa: F401
    _BUILTIN_EXCEPTION_KEYS,
    _Excuser,
    _fields,
    _fold_traced,
    _hit_outside,
    _id_name,
    _Joined,
    _Matcher,
    _nfkc_traced,
    _original_forms,
    _PhraseMatcher,
    _Traced,
    _traced_forms,
    deny_word_in,
    word_matches,
)
from uiprobe.guards.policy import (  # noqa: F401
    FIXED_HOME_APPS,
    SYSTEM_UI,
    AlsoTargetError,
    Decision,
    Detail,
    Judgement,
    Policy,
    api36_fallback_areas,
    check_also_target,
    edge_areas,
    parse_deny_file,
)
from uiprobe.guards.scroll import (
    changed_by_wheel,
    container_key,
    elements_at,
    is_dialog,
    lists_key,
    scroll_container,
    scroll_target,
    scrolled_container_on,
)
from uiprobe.guards.tree import (  # noqa: F401
    _CELLS_X,
    _CELLS_Y,
    _CONFIRM,
    _DENY,
    _EMPTY_AGG,
    _JOINED,
    _NONE,
    _PASSWORD_AT,
    _PERM,
    _PKG,
    _TEXT,
    _add_part,
    _Agg,
    _Grid,
    _is_dialog,
    _is_number_only,
    _is_slider,
    _is_text_field,
    _merge,
    _Tree,
)
from uiprobe.guards.words import (  # noqa: F401
    _BUILTIN_WORD_KEYS,
    _alone_in_join,
    _check_collection,
    _check_no_conflict,
    _check_word,
    _clean,
    _confirm_form,
    _deny_forms,
    _except_phrase_key,
    _exception_conflict,
    _exception_key,
    _exception_keys,
    _form_triple,
    _forms,
    _forms_match,
    _has_content,
    _in_word,
    _is_builtin_word,
    _is_combining,
    _is_default_ignorable,
    _is_invisible,
    _is_japanese_letter,
    _is_mark,
    _is_word_char,
    _join,
    _phrase_key,
    _separators_to_spaces,
    _split_camel,
    _stands_alone,
    _strip_invisible,
    _strip_marks,
    _tight_separators,
    _visible_end,
    _word_runs,
    normalize,
)
from uiprobe.model import MIN_VISIBLE_PX, Bounds  # noqa: F401

__all__ = [
    "API36_BOTTOM_PX",
    "API36_SIDE_PX",
    "API36_TOP_PX",
    "CONFIRM_WORDS",
    "DEFAULT_DENY_EXCEPTIONS",
    "DEFAULT_DENY_WORDS",
    "DEFAULT_SCROLLS_PER_SCREEN",
    "DEFAULT_SCROLL_AMOUNT",
    "DEFAULT_SCROLL_INCIDENTS",
    "DEFAULT_SCROLL_LISTS",
    "IRREVERSIBLE_PHRASES",
    "MAX_ELEMENTS",
    "MAX_SCROLL_AMOUNT",
    "PERMISSION_PACKAGES",
    "REASONS",
    "SCROLL_CONTAINER_CLASSES",
    "SCROLL_POINT_CLASSES",
    "Decision",
    "Detail",
    "Judgement",
    "Policy",
    "Reason",
    "api36_fallback_areas",
    "changed_by_wheel",
    "container_key",
    "deny_word_in",
    "edge_areas",
    "elements_at",
    "is_dialog",
    "judge",
    "lists_key",
    "normalize",
    "parse_deny_file",
    "scroll_container",
    "scroll_target",
    "scrolled_container_on",
    "word_matches",
]
