# Previewing a crawl

`uiprobe crawl PACKAGE` shows what a crawl of the app `PACKAGE` would press on the screen that is showing now, and for every clickable element it would not press, why not. The preview is the default: it presses nothing. The older form `uiprobe crawl PACKAGE --dry-run` still works and always means the preview, even when the settings file says `press = true`. `--press` and `press = true` ask for a pressing run; the pressing crawl does not exist yet, so such a run stops with a usage error (exit 2) before the adb server is contacted, after the settings file was read and checked: `pressing crawls are not available yet; this version only previews (run without --press)`. When the settings file says `press = true` and `--dry-run` is not given, the message names the file and says to add `--dry-run` or set `press = false` there. When `--press` is given as well, the message names the file and says to replace `--press` with `--dry-run`, or to remove `--press` and set `press = false` there (removing only `--press` would give exit 2 again, because the file still asks for pressing). `--press` together with `--dry-run` is a usage error (`--press and --dry-run cannot be combined`). The preview needs the settings file (see [Settings file](settings.md)): its words, rules, screen-edge sizes and smallest visible size are the defaults of the run. It reads `./uiprobe.toml` in the current folder, or the file named by `--config PATH`; no other folder is searched. Without a file the command stops with a usage error (exit 2) before the adb server is contacted:

```console
$ uiprobe crawl com.example.app
uiprobe: error: crawl needs a settings file and none was found (looked for ./uiprobe.toml)
create it with:    uiprobe init
or name one with:  uiprobe crawl com.example.app --config PATH
```

The last line keeps a `--dry-run` that was given (`uiprobe crawl com.example.app --dry-run --config PATH`), so that following it works with a file that says `press = true`; it also keeps a `--press` that was given (`uiprobe crawl com.example.app --press --config PATH`); that command still reports that pressing crawls are not available while `--press` is in it. With `--json` the three lines are the `message` of the `uiprobe.error/1` object, and `details` holds `looked_for`, `create` and `name_one`. When the missing file was named with `--config PATH`, the `create` line is `uiprobe init PATH` with the given path filled in (only when it needs no shell quoting; otherwise the word `PATH` stays a placeholder), so that it creates the file that was looked for. A file that is given with `--config` but missing, or that is invalid, is a usage error that names the file and, for an invalid entry, the key. Warnings about the file (an entry that has no effect) go to stderr.

```console
$ uiprobe crawl com.example.app
$ uiprobe crawl com.example.app --deny "gift card" --deny "coupon"
$ uiprobe crawl com.example.app --config ci/uiprobe.toml
$ uiprobe crawl com.example.app --tap-unlabeled
$ uiprobe crawl com.example.app --json
$ uiprobe crawl com.example.app --dry-run
```

Example output (abbreviated; it is not a recording of a real screen):

```console
$ uiprobe crawl com.example.shop
preview for com.example.shop on screen s-23512491bbaf; nothing was pressed
settings: uiprobe.toml
foreground: com.example.shop, keyboard: hidden
would press (1):
  e2 TextView "Browse" id=browse [tap] center=160,125
would not press (2):
  e3 TextView "Delete account" [tap] center=160,185: deny_word ("delete" in text)
  e6 TextView [tap] center=160,305: unlabeled
nothing was pressed: this was a preview (the default)
```

The last line says `(--dry-run)` instead of `(the default)` when `--dry-run` was given.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Force the preview (the default): judge the screen showing now; press nothing. Wins over `press = true` in the file. Cannot be combined with `--press`. |
| `--press` | Ask for a pressing run. Accepted but not available yet: it is refused with exit 2. Cannot be combined with `--dry-run`. |
| `--deny WORD` | Also never press an element that contains `WORD`, for this run only. May be given many times. |
| `--config PATH` | The settings file to use (default `./uiprobe.toml`). It must exist. |
| `--tap-unlabeled` | Allow pressing elements that have no text, description or hint, for this run, whatever the file says. This also allows icon-only buttons that deny words cannot see, such as a trash can. |

`--deny-file` no longer exists: the settings file replaces it, so giving it is a usage error. Put the words in the file's `[deny] words`. The [common options](commands.md#options) (`-s`, `--adb`, `--adb-server`, `--no-start-server`, `--json`) work as for the other commands.

What the preview does:

- It makes one dump of the screen showing now, then reads which app is in front, then whether the soft keyboard is shown. Apart from the temporary dump file that every dump writes on the device and tries to remove afterwards, on a best-effort basis (see [Network and privacy](network.md)), nothing that changes the device is sent: no input, no app launch. It creates no file or folder on this computer, also when it is interrupted (exit 130). Because it presses nothing, uiprobe does not refuse a real device; the [Safety](safety.md) advice about test devices still applies.
- It does not start the app or bring it to the front. If another app is in front, or the foreground cannot be read, every element is skipped as `outside_target_app`, except on a screen over the element limit, where every candidate gets only `screen_too_large` (see the table below). Open the app yourself first.
- Only clickable elements appear, under `would press` or `would not press` (`press` or `skip` in JSON); other elements are not listed. A candidate without a visible part of at least `[rules] min_visible_px` pixels (default 8) in width and height has no `center`; the fixed 8 pixels apply only to the center shown on a `screen_too_large` screen (see the table below). Two rules apply here and to no other command (`tap`, `find` and the rest are not affected): the crawl does not press an element whose label is only a number (`number_only`), and does not press a switch, checkbox, radio button or a row that holds one (`toggle`). Both only add refusals. An element that may be pressed is listed under `would press`; every other one is listed under `would not press` with all its reasons, in the fixed order below. For `deny_word` and `confirm_button` the line also says which word was found and where: `("delete" in text)` when the element itself holds it, `("delete" in text of e12)` when it was found in another element that shares the press point or belongs to the element's scope (for `confirm_button`, any element on the screen).
- The command exits with 0 whenever the preview was made, also when nothing may be pressed or another app is in front. Device errors exit as for `find`, and Ctrl+C exits with 130.

The reasons, in the order they are listed:

| Reason | Meaning |
| --- | --- |
| `screen_too_large` | The screen has more than 5000 elements. It is not judged, and every candidate gets only this reason. In the preview, the center shown for such a candidate is the middle of its visible part as `Screen.visible_bounds` gives it, which always uses 8 pixels, even when `[rules] min_visible_px` is larger (the element is still not pressed). |
| `outside_target_app` | The foreground is not `PACKAGE` (or is unknown), or the screen belongs to another package, or any element whose part on the display holds the press point belongs to another package, whether it is drawn on top of the candidate or behind it. |
| `permission_screen` | The press point belongs to a system permission dialog. |
| `not_visible` | No part of the element is on the display, or the part on the display is narrower or lower than `[rules] min_visible_px` pixels (default 8). |
| `covered` | The topmost meaningful element at the press point (clickable, or with a text, description or hint; the last in document order) is neither the element nor one of its descendants. The detail names that element (`element_id`). A candidate is always clickable, so it is always meaningful and is the topmost meaningful element at its own press point at the latest; a press target is therefore never missing for a candidate (the `no_target` refusal of `tap` cannot happen in a crawl). |
| `disabled` | The element is disabled. |
| `text_input` | Pressing the point would focus a text field and raise the keyboard. |
| `password_field` | The element or an element that holds its press point (an ancestor or something on top) is a password field, or the element or a clickable such element contains one. |
| `unlabeled` | The element and its descendants have no text, content description or hint (unless `--tap-unlabeled`). |
| `number_only` | The label is only a number: it has a numeral and otherwise only whitespace, punctuation and symbols (`200`, `$5`, `1,000`, `12:30`, `+1`; not `Page 2`, `8:30 AM`, `2 items`). A numeral is a digit of any script, a fraction such as `½` or a Roman numeral such as `Ⅳ`; the last two are refused on purpose. The label is the element's own text, else its description, else its hint, else the first non-empty text or description of a descendant. Such a tile is often a price (spending in-game coins) or a keypad digit. Over-blocks page number buttons, keypads and rating stars labelled `4.5`. |
| `toggle` | The element or a descendant is checkable (a switch, checkbox, radio button or toggle button), because pressing it changes an app setting. The detail names the checkable element. Over-blocks a Settings row whose main press opens a sub-screen but which also holds a switch. |
| `deny_word` | A deny word is found in the element's scope: its own fields and its descendants, the descendants of every clickable ancestor, and every element that holds its press point (an ancestor or something on top): its own fields and the descendants of its clickable ancestors, and, when it is clickable, all its descendants. The scope also includes the joined text and descriptions of the subtree of each clickable element in it, so a word split over children matches (separate children "Log" and "out" match `log out`); the JSON detail then has the field `joined`. |
| `irreversible_screen` | The screen warns that something cannot be taken back: the text, description or hint of any element on it holds an irreversibility phrase (see below). Every element of the screen gets this reason, whatever its own label is, so that neither the "Yes" nor the "Cancel" button of such a screen is pressed. The detail names the phrase (`word`), the field and the element where it was found (`element_id`). It is switched off by `[rules] refuse_irreversible_screens = false`. |
| `confirm_button` | The element, or a clickable element holding its press point, is labeled as a confirm button and a deny word is found somewhere on the screen. A label is a confirm label when it equals a confirm word ("OK", "Yes", "Allow", "はい", ...) or starts with one followed by a character that is not a letter ("Yes I'm sure!", "OK, delete", "Continue anyway"); "Okinawa", "Yesterday" and "Continued" are not. The check applies to full-screen layouts as well as dialogs. |
| `dialog_confirm_button` | The element is a confirm button on a dialog (unless `[rules] refuse_dialog_confirm` is `false`). |
| `screen_edge` | The press point lies in a screen-edge area where a press would pull the status bar, a gesture or the navigation bar. |

Deny words are matched as substrings in the text, content description, hint and resource id (without its package part), ignoring case, width variants and separators, so `signUp`, `sign-up` and `SIGN UP` all match `sign up`. The built-in list holds Japanese and English words for buying, paying, sending, posting, deleting, disabling, force-stopping, signing in, signing out, adding to a cart, opening an account, starting over (`restart`, `undo`, `やり直`, `元に戻`) and similar actions.

Irreversibility phrases are matched like deny words: as substrings, ignoring case, width variants, separators and invisible characters, and a curly apostrophe counts as a plain one. Each of the text, description and hint of every element is matched alone; the resource id is not read, and a phrase split over two elements is not found. The built-in list is `can't undo`, `cannot undo`, `can not be undone`, `cannot be undone`, `can't be undone`, `irreversible`, `permanently`, `this action is permanent`, `元に戻せません`, `取り消せません`, `元に戻すことはできません`, `取り消すことはできません` and `完全に削除`. The list is in the settings file (`[irreversible] phrases`), so you can remove a phrase there. In the order of the reasons, `irreversible_screen` comes right after `deny_word` and before `confirm_button`.

The screen-edge areas come from the settings file; the generated file holds the sizes measured on an API 36 emulator (24 px at the top, 32 px at the bottom, 30 px at each side), the same for every device. They are pixels, not scaled to the device's density. The smallest visible size (`min_visible_px`, 8 in the generated file) applies to the rules of the crawl preview only: `tap`, `find` and the Python `Screen` keep the fixed 8 pixels.

Limits that are known and accepted:

- Substrings over-block: `Posture` contains `post` and `Recorder` contains `order`, so both are not pressed. A miss is worse than an over-block.
- The sign-in, disable and force-stop words over-block too: `sign in` matches `Designing`, `Signing out` and `Design info`, `log in` matches `Dialog input` (any word ending in `log` or `sign` before a word starting with `in`), `disable` matches a `Disabled` status text, and `無効` matches a status text such as `Wi-Fi 無効` in a Settings row. The cart and account words over-block too: `account` matches `Accounts` and `Accounting`, `cart` matches `Cartoon`, `Descartes` and, across a space, `Music artists`.
- A non-clickable container whose resource id or description holds a deny word (for example a list called `order_history_list`) blocks every element under it.
- A deny label that is a sibling of a clickable icon in a row that itself is not clickable is not part of the icon's scope.
- A password field below a non-clickable container that holds the press point does not stop an element from being pressed.
- The start-over words over-block: an `Undo` button that only reverts the last edit, or a `Restart` button of a media player, is refused too, and so are `Restarted`, `Undone`, `Undoubtedly` and `Undock`. The lists prefer refusing too much.
- A confirm word at the start of a label needs a non-letter after it, so `Yes I'm sure!` counts and `Yesterday` does not. A word that ends in a letter without case (Unicode category Lo: hiragana, kanji, hangul and most katakana) needs no boundary, because these scripts are written without spaces: `はい、削除します` and `はい削除します` both count, and so do `はいる`, `確認画面` and the negative `許可しない` (over-blocks). A word ending in the katakana long-vowel mark `ー` still needs a boundary, and a confirm word in Latin letters is not matched when Japanese follows it (`OKです`).
- The irreversibility phrases over-block: `permanently` matches `Permanently moved` and a settings text such as `Permanently allowed`, and a screen with one such text is refused as a whole, even its harmless buttons. The reverse is also true: a warning worded in another way (`This is final`, `No going back`) is not recognised.
- Katakana `ハイ` (or half-width `ﾊｲ`) is not a confirm word; only hiragana `はい` is.
- The preview judges one screen as it is. It cannot see what a press would lead to, and it does not look at the pixels, only at what `uiautomator` reports.

The output with `--json` is a `uiprobe.crawl_preview/1` object (see [Output formats](output.md)).
