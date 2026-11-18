# Previewing a crawl

`uiprobe crawl PACKAGE --dry-run` shows what a crawl of the app `PACKAGE` would press on the screen that is showing now, and for every clickable element it would not press, why not. Only `--dry-run` exists so far; `uiprobe crawl PACKAGE` without it is refused with a usage error (exit 2).

```console
$ uiprobe crawl com.example.app --dry-run
$ uiprobe crawl com.example.app --dry-run --deny "gift card" --deny "coupon"
$ uiprobe crawl com.example.app --dry-run --deny-file deny.txt
$ uiprobe crawl com.example.app --dry-run --tap-unlabeled
$ uiprobe crawl com.example.app --dry-run --json
```

Example output (abbreviated; it is not a recording of a real screen):

```console
$ uiprobe crawl com.example.shop --dry-run
preview for com.example.shop on screen s-23512491bbaf; nothing was pressed
foreground: com.example.shop, keyboard: hidden
would press (1):
  e2 TextView "Browse" id=browse [tap] center=160,125
would not press (2):
  e3 TextView "Delete account" [tap] center=160,185: deny_word ("delete" in text)
  e6 TextView [tap] center=160,305: unlabeled
```

| Option | Meaning |
| --- | --- |
| `--dry-run` | Required so far. Judge the screen showing now; press nothing. |
| `--deny WORD` | Also never press an element that contains `WORD`. May be given many times. |
| `--deny-file PATH` | Also add the words of a file (format below). May be given many times; the words of every file are added. |
| `--tap-unlabeled` | Allow pressing elements that have no text, description or hint. This also allows icon-only buttons that deny words cannot see, such as a trash can. |

The [common options](commands.md#options) (`-s`, `--adb`, `--adb-server`, `--no-start-server`, `--json`) work as for the other commands.

What the preview does:

- It makes one dump of the screen showing now, then reads which app is in front, then whether the soft keyboard is shown. Apart from the temporary dump file that every dump writes on the device and tries to remove afterwards, on a best-effort basis (see [Network and privacy](network.md)), nothing that changes the device is sent: no input, no app launch. It creates no file or folder on this computer, also when it is interrupted (exit 130). Because it presses nothing, uiprobe does not refuse a real device; the [Safety](safety.md) advice about test devices still applies.
- It does not start the app or bring it to the front. If another app is in front, or the foreground cannot be read, every element is skipped as `outside_target_app`, except on a screen over the element limit, where every candidate gets only `screen_too_large` (see the table below). Open the app yourself first.
- Only clickable elements appear, under `would press` or `would not press` (`press` or `skip` in JSON); other elements are not listed. A candidate without a visible part of at least 8 x 8 pixels has no `center`. Two rules apply here and to no other command (`tap`, `find` and the rest are not affected): the crawl does not press an element whose label is only a number (`number_only`), and does not press a switch, checkbox, radio button or a row that holds one (`toggle`). Both only add refusals. An element that may be pressed is listed under `would press`; every other one is listed under `would not press` with all its reasons, in the fixed order below. For `deny_word` and `confirm_button` the line also says which word was found and where: `("delete" in text)` when the element itself holds it, `("delete" in text of e12)` when it was found in another element that shares the press point or belongs to the element's scope (for `confirm_button`, any element on the screen).
- The command exits with 0 whenever the preview was made, also when nothing may be pressed or another app is in front. Device errors exit as for `find`, and Ctrl+C exits with 130.

The reasons, in the order they are listed:

| Reason | Meaning |
| --- | --- |
| `screen_too_large` | The screen has more than 5000 elements. It is not judged, and every candidate gets only this reason. |
| `outside_target_app` | The foreground is not `PACKAGE` (or is unknown), or the screen belongs to another package, or any element whose part on the display holds the press point belongs to another package, whether it is drawn on top of the candidate or behind it. |
| `permission_screen` | The press point belongs to a system permission dialog. |
| `not_visible` | No part of the element is on the display, or the part on the display is under 8 pixels wide or under 8 pixels high. |
| `covered` | The topmost meaningful element at the press point (clickable, or with a text, description or hint; the last in document order) is neither the element nor one of its descendants. The detail names that element (`element_id`). A candidate is always clickable, so it is always meaningful and is the topmost meaningful element at its own press point at the latest; a press target is therefore never missing for a candidate (the `no_target` refusal of `tap` cannot happen in a crawl). |
| `disabled` | The element is disabled. |
| `text_input` | Pressing the point would focus a text field and raise the keyboard. |
| `password_field` | The element or an element that holds its press point (an ancestor or something on top) is a password field, or the element or a clickable such element contains one. |
| `unlabeled` | The element and its descendants have no text, content description or hint (unless `--tap-unlabeled`). |
| `number_only` | The label is only a number: it has a numeral and otherwise only whitespace, punctuation and symbols (`200`, `$5`, `1,000`, `12:30`, `+1`; not `Page 2`, `8:30 AM`, `2 items`). A numeral is a digit of any script, a fraction such as `½` or a Roman numeral such as `Ⅳ`; the last two are refused on purpose. The label is the element's own text, else its description, else its hint, else the first non-empty text or description of a descendant. Such a tile is often a price (spending in-game coins) or a keypad digit. Over-blocks page number buttons, keypads and rating stars labelled `4.5`. |
| `toggle` | The element or a descendant is checkable (a switch, checkbox, radio button or toggle button), because pressing it changes an app setting. The detail names the checkable element. Over-blocks a Settings row whose main press opens a sub-screen but which also holds a switch. |
| `deny_word` | A deny word is found in the element's scope: its own fields and its descendants, the descendants of every clickable ancestor, and every element that holds its press point (an ancestor or something on top): its own fields and the descendants of its clickable ancestors, and, when it is clickable, all its descendants. The scope also includes the joined text and descriptions of the subtree of each clickable element in it, so a word split over children matches (separate children "Log" and "out" match `log out`); the JSON detail then has the field `joined`. |
| `confirm_button` | The element, or a clickable element holding its press point, is labeled as a confirm button ("OK", "Yes", "Allow", "はい", ...) and a deny word is found somewhere on the screen. |
| `dialog_confirm_button` | The element is a confirm button on a dialog. |
| `screen_edge` | The press point lies in a screen-edge area where a press would pull the status bar, a gesture or the navigation bar. |

Deny words are matched as substrings in the text, content description, hint and resource id (without its package part), ignoring case, width variants and separators, so `signUp`, `sign-up` and `SIGN UP` all match `sign up`. The built-in list holds Japanese and English words for buying, paying, sending, posting, deleting, disabling, force-stopping, signing in, signing out, adding to a cart, opening an account and similar actions.

The deny file is UTF-8, with or without a byte order mark, with LF or CRLF line ends (as Windows Notepad writes them). One word per line; blank lines and lines starting with `#` are skipped, and each word is stripped. UTF-16 files, invalid UTF-8 and control characters other than tab are refused, and so is a file larger than 1 MiB. The file must be a regular file: a directory, a pipe or a device is refused without being read, and a file that changes while it is read is refused (size, modification time and change time are compared; the change time is the creation time on Windows, so a same-size rewrite with a restored modification time is caught on Linux and macOS when the file system records a newer change time, but not on Windows). All of these are usage errors, found before the adb server is contacted. One case remains: a file swapped for a pipe or a device exactly between the check and the read, or a file that keeps growing meanwhile, can still make the command wait or keep reading until Ctrl+C (exit 130). A path that is switched to another file during the read and switched back (through a symbolic link or a renamed folder, for example) is not detected.

The screen-edge areas use the sizes measured on an API 36 emulator (24 px at the top, 32 px at the bottom, 30 px at each side) for every device for now. They are pixels, not scaled to the device's density.

Limits that are known and accepted:

- Substrings over-block: `Posture` contains `post` and `Recorder` contains `order`, so both are not pressed. A miss is worse than an over-block.
- The sign-in, disable and force-stop words over-block too: `sign in` matches `Designing`, `Signing out` and `Design info`, `log in` matches `Dialog input` (any word ending in `log` or `sign` before a word starting with `in`), `disable` matches a `Disabled` status text, and `無効` matches a status text such as `Wi-Fi 無効` in a Settings row. The cart and account words over-block too: `account` matches `Accounts` and `Accounting`, `cart` matches `Cartoon`, `Descartes` and, across a space, `Music artists`.
- A non-clickable container whose resource id or description holds a deny word (for example a list called `order_history_list`) blocks every element under it.
- A deny label that is a sibling of a clickable icon in a row that itself is not clickable is not part of the icon's scope.
- A password field below a non-clickable container that holds the press point does not stop an element from being pressed.
- Katakana `ハイ` (or half-width `ﾊｲ`) is not a confirm word; only hiragana `はい` is.
- The preview judges one screen as it is. It cannot see what a press would lead to, and it does not look at the pixels, only at what `uiautomator` reports.

The output with `--json` is a `uiprobe.crawl_preview/1` object (see [Output formats](output.md)).
