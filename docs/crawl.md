# Crawling an app

`uiprobe crawl PACKAGE` shows what a crawl of the app `PACKAGE` would press on the screen that is showing now, and for every clickable element it would not press, why not. The preview is the default: it presses nothing. The older form `uiprobe crawl PACKAGE --dry-run` still works and always means the preview. `uiprobe crawl PACKAGE --press --out DIR` crawls for real: see [Pressing crawl](#pressing-crawl---press). A crawl presses only when `--press` is given: when the settings file says `press = true` and neither `--press` nor `--dry-run` is given, the command stops with a usage error (exit 2) before the adb server is contacted, naming the file and saying to add `--press` and `--out DIR` to press, or `--dry-run` to preview. `--press` together with `--dry-run` is a usage error (`--press and --dry-run cannot be combined`), and so is `--press` without `--out`. The preview needs the settings file (see [Settings file](settings.md)): its words, rules, screen-edge sizes and smallest visible size are the defaults of the run. It reads `./uiprobe.toml` in the current folder, or the file named by `--config PATH`; no other folder is searched. Without a file the command stops with a usage error (exit 2) before the adb server is contacted:

```console
$ uiprobe crawl com.example.app
uiprobe: error: crawl needs a settings file and none was found (looked for ./uiprobe.toml)
create it with:    uiprobe init
or name one with:  uiprobe crawl com.example.app --config PATH
```

The last line keeps a `--dry-run` that was given (`uiprobe crawl com.example.app --dry-run --config PATH`), so that following it works with a file that says `press = true`; with `--press` it says `add --config PATH to the same command` instead, because a pressing run has more options than the line could repeat. With `--json` the three lines are the `message` of the `uiprobe.error/1` object, and `details` holds `looked_for`, `create` and `name_one`. When the missing file was named with `--config PATH`, the `create` line is `uiprobe init PATH` with the given path filled in (only when it needs no shell quoting; otherwise the word `PATH` stays a placeholder), so that it creates the file that was looked for. A file that is given with `--config` but missing, or that is invalid, is a usage error that names the file and, for an invalid entry, the key. Warnings about the file (an entry that has no effect) go to stderr.

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
| `--press` | Crawl for real (see [Pressing crawl](#pressing-crawl---press)). Needs `--out DIR`. Cannot be combined with `--dry-run`. |
| `--deny WORD` | Also never press an element that contains `WORD`, for this run only. May be given many times. |
| `--config PATH` | The settings file to use (default `./uiprobe.toml`). It must exist. |
| `--tap-unlabeled` | Allow pressing elements that have no text, description or hint, for this run, whatever the file says. This also allows icon-only buttons that deny words cannot see, such as a trash can. |

`--deny-file` no longer exists: the settings file replaces it, so giving it is a usage error. Put the words in the file's `[deny] words`. The [common options](commands.md#options) (`-s`, `--adb`, `--adb-server`, `--no-start-server`, `--json`) work as for the other commands.

What the preview does:

- It makes one dump of the screen showing now, then reads which app is in front, then whether the soft keyboard is shown. Apart from the temporary dump file that every dump writes on the device and tries to remove afterwards, on a best-effort basis (see [Network and privacy](network.md)), nothing that changes the device is sent: no input, no app launch. It creates no file or folder on this computer, also when it is interrupted (exit 130). Because it presses nothing, uiprobe does not refuse a real device; the [Safety](safety.md) advice about test devices still applies.
- It does not start the app or bring it to the front. If another app is in front, or the foreground cannot be read, every element is skipped as `outside_target_app`, except on a screen over the element limit, where every candidate gets only `screen_too_large` (see the table below). Open the app yourself first.
- Only clickable elements appear, under `would press` or `would not press` (`press` or `skip` in JSON); other elements are not listed. A candidate without a visible part of at least `[rules] min_visible_px` pixels (default 8) in width and height has no `center`; the fixed 8 pixels apply only to the center shown on a `screen_too_large` screen (see the table below). Two rules apply here and to no other command (`tap`, `find` and the rest are not affected): the crawl does not press an element whose label is only a number (`number_only`), and does not press a switch, checkbox, radio button or a row that holds one (`toggle`). Both only add refusals. An element that may be pressed is listed under `would press`; every other one is listed under `would not press` with all its reasons, in the fixed order below. For `deny_word`, `irreversible_screen` and `confirm_button` the line also says which word or phrase was found and where: `("delete" in text)` when the element itself holds it, `("delete" in text of e12)` when it was found in another element that shares the press point or belongs to the element's scope (for `confirm_button` and `irreversible_screen`, any element on the screen).
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
| `irreversible_screen` | The screen warns that something cannot be taken back: the text, description or hint of any element on it holds an irreversibility phrase (see below). Every element of the screen gets this reason, whatever its own label is, so that neither the "Yes" nor the "Cancel" button of such a screen is pressed. The detail names the phrase (`word`, with curly apostrophes written as plain ones), the field and the element where it was found (`element_id`), the first one in document order. Hidden and off-screen elements count too. It is switched off by `[rules] refuse_irreversible_screens = false`. |
| `confirm_button` | The element, or a clickable element holding its press point, is labeled as a confirm button and a deny word is found somewhere on the screen (exception words never apply here). A label is a confirm label when it equals a confirm word ("OK", "Yes", "Allow", "はい", ...) or starts with one followed by a character that is not a letter ("Yes I'm sure!", "OK, delete", "Continue anyway"); "Okinawa", "Yesterday" and "Continued" are not. The check applies to full-screen layouts as well as dialogs. |
| `dialog_confirm_button` | The element is a confirm button on a dialog (unless `[rules] refuse_dialog_confirm` is `false`). |
| `screen_edge` | The press point lies in a screen-edge area where a press would pull the status bar, a gesture or the navigation bar. |

Deny words are matched as substrings in the text, content description, hint and resource id (without its package part), ignoring case, width variants and separators, so `signUp`, `sign-up` and `SIGN UP` all match `sign up`. The built-in list holds Japanese and English words for buying, paying, sending, posting, deleting, disabling, force-stopping, signing in, signing out, adding to a cart, opening an account, starting over (`restart`, `undo`, `やり直`, `元に戻`) and similar actions.

Exception words are empty by default, so by default the deny words decide exactly as described above. You can list words under `[deny] except` in the settings file. A deny word found completely inside such a word, where it stands as one whole word of a text, content description or hint (also in the texts of an element's descendants read together), then no longer counts. For example, with `except = ["automatically"]`, `call` inside `Automatically` no longer refuses a Settings row whose summary reads `Wi-Fi doesn't turn back on automatically`. Only that occurrence is excused: an element whose only hit was there is then pressed, also by `crawl --press`, unless another reason refuses it or the limits below apply. `Automatically pay` is still refused by `pay`, and a label split across child elements may still be refused (see below), but `Bill my card automatically`, `Empty trash automatically` and `Redial automatically` hold no other hit and are pressed. Whoever adds an exception word takes that on. To see what the deny words matched, look at the elements that would not be pressed: each line of the preview names the word, the field and the element (`deny_word ("call" in text of e5)`), and `--json` has them under `details`; in a pressing run, `skipped.jsonl` has the reasons and, for `deny_word`, `irreversible_screen` and `confirm_button`, the same details (see [The run folder](run-folder.md)). It has one line per screen id, element fingerprint and reasons, not one per element: identical elements of one screen with the same reasons share a line (the details are those of the first one), and a screen with too many elements to judge gives one line for the whole screen (`screen_too_large`).

When an exception word excuses a hit, exactly: the hit lies completely inside an occurrence of the exception word; that occurrence is one whole word (a run of letters and digits, with the accent marks that belong to them, equal to the exception word apart from case and width variants) inside the text of one element field (its text, content description or hint); and it has real separators on both sides, that is a space, punctuation, or the start or end of that text. Where the texts of an element's descendants are read together, the start or end of one text counts as a separator only when the nearest visible character of the neighbouring text is not a letter, digit or combining mark (texts with only invisible characters are passed over). Every occurrence of a deny word is checked on its own, so `Recorder order` is still refused with `recorder` listed (the second `order` stands outside the word). Every clickable element in the subtree is judged this way, so an outer clickable container cannot hide the refusal of a button inside it.

When it does not excuse a hit (each of these refuses more, never less): a hit that only overlaps the occurrence or spans two texts; an exception word that appears only across two texts or touches a letter, digit or mark of the neighbouring text, so a label split across child elements may not be excused (`recorder` + `s`, also with `recorders` listed; `recorder` + U+0301; U+0332 + `recorder`; `削除済み` + `にする`; and `Turn on` + `automatically`, although a person reads two words there); and a word that is not the same once normalized: `Borders` is another word than `border`, a word that appears only once spaces are removed does not count (`Job order` still matches `order`, although `joborder` holds `border`), and an invisible character or a full-width or circled letter inside the word, an additional or different combining mark (an accent) inside it or right after it, a boundary that exists only after width folding (`Ŀrecorder` is one word, although folding turns `Ŀ` into `L` and a middle dot) and an invisible character next to it that hides a longer word (`recorder` + zero-width space + `s` reads `recorders`; `recorder` + zero-width space alone, or followed by a space, keeps the exception) all make it another word. In these cases any deny hit that is still present after normalization counts. A hit that normalization removes was never a hit, with or without exceptions: the single text `recorder` + U+0301 holds no `order` once width folding composes `ŕ`. An exception word written with a mark (`r` + U+0308 + `ecorder`) matches the text with the same mark, unless width folding (NFKC) composes the mark with its letter into one character (`e` + U+0301 becomes `é`): then the exception does not match and the hit counts.

An entry must be one word of letters and digits without invisible characters (a zero-width space or a variation selector such as U+FE0F is refused) and must not equal a deny word; such a pair is an error that says to remove the deny word instead. An exception word excuses every deny word, the built-in ones and the ones added in the file or with `--deny`; a word given with `--deny` that equals an exception word blocks as given, because the exception is dropped for that run. Resource ids, the irreversibility phrases and the screen-wide search behind `confirm_button` never use exception words, so `Debit $50 automatically?` on a screen still refuses its `Continue` button.

Limits of exception words: the rules above are what uiprobe checks, and they lean towards refusing. Combinations of text an app crafts with exception words you write can still get through in these known ways, which are accepted limits, not checked: a compatibility character that width folding turns into a letter, at the start of the next text, is not seen as continuing the word (with `recorder` listed, the texts `recorder` + `ⓢ` are pressed, while the single text `recorderⓢ` is refused); a combining mark at the start of a text, after punctuation at the end of the text before it, is passed over (with `recorder` listed, `!` + U+0332 + `recorder` in two texts is pressed); case folding can create the hit that the exception then excuses (with `reassigning` listed, `reaßigning` is pressed, because folding `ß` gives `ss` and so `sign in`, which without the exception refuses it). The other way round, a whole exception word can still be refused, so a button holding it is not always pressed: when a neighbouring text changes under width folding, uiprobe cannot trace the joined text back to the texts and counts every hit in it (with `recorder` listed, the texts `Cafe` + U+0301 + `. ` and `Recorder` are refused for `order`, while `Café. ` written with the precomposed `é` and `Recorder` are pressed). An exception word you add means that a button containing that word, where it stands whole, is pressed when no other deny word matches it.

Irreversibility phrases are matched like deny words: as substrings, ignoring case, width variants, separators and invisible characters, and a curly apostrophe counts as a plain one. Each of the text, description and hint of every element is matched alone; the resource id is not read, and a phrase split over two elements is not found. Because spaces and separators are ignored, `cannot be undone` also matches `can not be undone`. The built-in list is `can't undo`, `cannot undo`, `cannot be undone`, `can't be undone`, `cannot be reversed`, `can't be reversed`, `cannot be recovered`, `can't be recovered`, `irreversible`, `irreversibly`, `permanently`, `this action is permanent`, `元に戻せません`, `取り消せません`, `元に戻すことはできません`, `元に戻すことができません`, `取り消すことはできません`, `取り消すことができません`, `復元できません` and `完全に削除`. Only these phrases (and the ones you add) are recognized, not every way of saying that something cannot be undone. The list is in the settings file (`[irreversible] phrases`), so you can remove a phrase there. In the order of the reasons, `irreversible_screen` comes right after `deny_word` and before `confirm_button`.

The screen-edge areas come from the settings file; the generated file holds the sizes measured on an API 36 emulator (24 px at the top, 32 px at the bottom, 30 px at each side), the same for every device. They are pixels, not scaled to the device's density. The smallest visible size (`min_visible_px`, 8 in the generated file) applies to the rules of the crawl preview only: `tap`, `find` and the Python `Screen` keep the fixed 8 pixels.

Limits that are known and accepted:

- Substrings over-block: `Posture` contains `post` and `Recorder` contains `order`, so both are not pressed. A miss is worse than an over-block. An exception word in the settings file can let such a word through, with the risk described above.
- The sign-in, disable and force-stop words over-block too: `sign in` matches `Designing`, `Signing out` and `Design info`, `log in` matches `Dialog input` and `Catalog in stock` (any word ending in `log` or `sign` before a word starting with `in`), `disable` matches a `Disabled` status text, and `無効` matches a status text such as `Wi-Fi 無効` in a Settings row. The cart and account words over-block too: `account` matches `Accounts` and `Accounting`, `cart` matches `Cartoon`, `Descartes` and, across a space, `Music artists`. Exception words do not help where the hit spans two words, as in `Dialog input`, `Catalog in stock` and `Music artists`.
- An exception word lets through every label in which it stands whole and no other deny word matches, including labels that name an action: with `automatically` listed, `Bill my card automatically`, `Dial automatically` and `Forward automatically` are pressed; with `recorder`, `Email recorder audio`. The deny list has no words such as `dial`, `charge` or `renew` that would catch these.
- A non-clickable container whose resource id or description holds a deny word (for example a list called `order_history_list`) blocks every element under it.
- A deny label that is a sibling of a clickable icon in a row that itself is not clickable is not part of the icon's scope.
- A password field below a non-clickable container that holds the press point does not stop an element from being pressed.
- The start-over words over-block: an `Undo` button that only reverts the last edit, or a `Restart` button of a media player, is refused too, and so are `Restarted`, `Undone`, `Undoubtedly` and `Undock`. The lists prefer refusing too much.
- A confirm word at the start of a label needs a non-letter after it, so `Yes I'm sure!` counts and `Yesterday` does not. A word that ends in a letter of the Japanese script (hiragana, katakana including the long-vowel mark `ー` and the iteration marks, kanji including `々`) needs no boundary, because Japanese is written without spaces: `はい、削除します` and `はい削除します` both count, a custom word `オーケー` matches `オーケーです`, and so do `はいる`, `確認画面` and the negative `許可しない` (over-blocks). Words in every other script keep the boundary, and a confirm word in Latin letters is not matched when Japanese follows it (`OKです`). A label split over several text elements is read joined by a space and, where one text ends and the next starts with a Japanese letter, also joined without one (`確` + `認してください`).
- The irreversibility phrases over-block: `permanently` matches `Permanently moved` and a settings text such as `Permanently allowed`, and a screen with one such text is refused as a whole, even its harmless buttons. The reverse is also true: a warning worded in another way (`This is final`, `No going back`) is not recognised.
- Katakana `ハイ` (or half-width `ﾊｲ`) is not a confirm word; only hiragana `はい` is.
- The preview judges one screen as it is. It cannot see what a press would lead to, and it does not look at the pixels, only at what `uiautomator` reports.

The output with `--json` is a `uiprobe.crawl_preview/1` object (see [Output formats](output.md)).

## Pressing crawl (`--press`)

`uiprobe crawl PACKAGE --press --out DIR` presses for real on the selected device. It starts the app, judges every screen with the same rules as the preview, taps what they allow, presses BACK, and restarts the app when it is stuck or another app comes to the front. Every step is recorded in the new folder `DIR` (see [Run folder](run-folder.md)). It is the command form of `crawl_run()` (see [Crawling from Python](python.md#crawling-from-python-alpha)), which describes each check it makes before a tap.

```console
$ uiprobe crawl com.example.app --press --out runs/first
$ uiprobe crawl com.example.app --press --out runs/long --actions 200 --minutes 20
$ uiprobe crawl com.example.app --press --out runs/real --allow-real-device
```

Safe defaults:

- It presses only on an emulator. A device that is not an emulator, or whose emulator check (`getprop ro.kernel.qemu`) cannot be read, is refused with exit 17 (`not_emulator`, `details.reason` `not_emulator` or `unreadable`) before anything is written or sent; the message says to add `--allow-real-device`, which presses on that device.
- It stops soon: after 30 sent actions (the launch, taps, BACK and restarts each count; an action the checks held back does not) or 5 minutes, unless `--actions` or `--minutes` say otherwise. `--depth` (default 3) limits how deep it taps on one path: on a screen that deep it presses BACK and goes on with another path, so the depth does not end the run (when the current path is a single screen it sends no navigation BACK, only a BACK that closes a shown keyboard: with `--depth 1`, once the first screen is judged with nothing deeper allowed, the run ends `exhausted`, unless another stop reason such as `keyboard_stuck` or `max_actions` comes first). The time limit is measured from the start of the run, set once: an action is not sent once it has passed, the launch and restarts included, as it is checked by uiprobe's own clock on the request's own connection right before the request is written. Reads are not cut off: a read in progress (a dump, for example) can run until it finishes after the limit, and the run then ends at the next step. The settings file's `[limits]` are not used by `--press`, because the generated file writes larger values for every run.
- It sends nothing to a screen of another app: it restarts the target app instead (`am force-stop`, then `am start -W`), which can lose unsaved state in that app. `--back-on-other-screens` makes it press BACK there instead; that BACK reaches whichever app has the focus.
- It starts the app with `am start -W` of its launcher activity, which it reads with `cmd package resolve-activity` right before each start (the first launch and every relaunch, after its force-stop; no component is kept between starts). The time limit is checked again after that read, so a read that ends past the limit sends no start. The previous version started apps with `monkey`, which turned auto-rotate on (seen on API 24 and API 36 emulators and a Samsung API 36 phone); `am start` does not change it (see [Launching an app](commands.md#launching-an-app)). When the launcher activity cannot be read, the start is not sent and the crawl ends with exit 15 (`launch_failed`, `launcher_unreadable`; the launch or relaunch gets a `not_sent` end line, or `start_not_sent`). The time limit counts from right after the app's uid is read, so the launcher read is inside it. A start whose reply names an activity of another app is waited on like any start: when the app comes to the front within the launch timeout the crawl goes on with it; otherwise that other app counts as in front (the crawl goes on, and relaunches). A start whose reply does not come in time ends the crawl (exit 15, `start_timeout`): whether the app started is unknown, so nothing more is sent.
- Right before each tap it reads which window takes a touch at the tap's point and sends the tap only when that window belongs to the app. `--no-tap-window-check` turns this off; then an overlay of another app over the point gets the tap.
- The time limit is checked one last time by uiprobe's own clock right before a tap, BACK, force-stop or start is written, so slow checks cannot carry it past the limit. From Python (`crawl_run()`), nothing you pass runs between the last check and such a request; a `stop` that turns true while the checks before a tap or BACK read the screen takes effect after that one press (an accepted limit), as `stop` is asked again when the next observation begins.
- The device is pinned when the run starts: every request goes to that device, and a renumbered connection to the same device is followed (when its boot id can be read) or ends the run. Without a readable boot id, a renumbered connection and any failed adb exchange with the device, even on the same connection, end the run, since the device can then not be told apart from another emulator of the same image.

Before the first action it prints what it will do (to stderr with `--json`, so that stdout holds only the JSON result), and the lines are written out before the app is started: the device and whether it is an emulator, the app, the limits, the run folder, and the two choices above. At the end it prints why it stopped (`exhausted`, `max_actions`, `max_duration`, `relaunch_limit`, `keyboard_stuck`, `dump_failed_repeatedly`, and the other stop reasons of [Run folder](run-folder.md)), the number of actions started (those not sent included, so it can be larger than `--actions`) and of screens, the actions that were not sent, by reason (for example `covered_by_other`, `cover_unknown`, `device_reconnected`), the number of transport moves, and the run folder:

```console
$ uiprobe crawl com.example.shop --press --out runs/first
pressing on emulator-5554 (emulator): com.example.shop
limits: depth 3, actions 30, minutes 5
run folder: runs/first
BACK on screens of other apps: no (restart the app)
check the window at each tap point: yes
stopped: max_actions
actions: 31 (started; those not sent included)
screens: 7
not sent: covered_by_other 1
transport moves: 0
run folder: runs/first
```

Lines starting with `note:` follow when one of these happened:

- A tap was refused because the window at its point could not be read (`cover_unknown`): the note names what could not be read and the option that turns the check off, with its risk. When the window list reader refused it, the note also names its rule and suggests `uiprobe diagnose tap-window` (see [Diagnosing the tap window check](commands.md#diagnosing-the-tap-window-check)); a refusal made before the list was read (the gate's own dump rotated, the app's uid) has no rule and no such suggestion. With the check on (the default), a rotated display prevents every tap; when another check fails as well (for example the keyboard came up), that reason may be the one recorded.
- The device's boot id could not be read: a renumbered connection then ends the run instead of being followed, and so does any failed adb exchange with the device, even on the same connection.
- The run stopped with `dump_failed_repeatedly`: `uiautomator dump` fails on a screen that never settles (a value that changes every second, an animation). The note names the app in front and the last screen that was read, so you can avoid that screen.

| Option | Meaning |
| --- | --- |
| `--out DIR` | The new run folder. Required with `--press`; it must not exist. |
| `--actions N` | The most sent actions, 1 to 10000 (default 30). |
| `--depth N` | How deep it taps on one path, 1 to 50 (default 3); it does not end the run. |
| `--minutes N` | The longest run in minutes, 1 to 1440 (default 5). |
| `--settle SEC` | The pause after a tap or BACK, 0 to 60 seconds (default 1). |
| `--screenshots` | Also save a screenshot of each new screen in the run folder. |
| `--allow-real-device` | Also press on a device that is not an emulator. |
| `--back-on-other-screens` | Press BACK on a screen of another app instead of restarting the app. |
| `--no-tap-window-check` | Do not read which window takes a tap at its point. |

`--deny`, `--tap-unlabeled` and `--config` work as for the preview. The options above are used only with `--press`; given without it, each is a usage error. Every value of `--actions`, `--depth`, `--minutes` and `--settle` is checked as it is read, so a wrong value is a usage error even when the option is given again later with a good one.

Exit codes: 0 when the run ended by one of its stop reasons, also when nothing could be pressed; 130 on Ctrl+C, after the run folder's `run.json` was written with the stop reason `interrupted` when that was still possible (the message names the folder when it exists; a second Ctrl+C while `run.json` is written can leave the folder without it); 17 for a refused device; the other errors as for the other commands, with `details.out` naming the run folder when it was created. With `--json` the result is a `uiprobe.crawl_run/1` object on stdout (see [Output formats](output.md)), which holds the plan as well; the plan lines go to stderr then. If stdout (or, with `--json`, stderr) is closed when the command starts, it stops with a usage error before anything is sent. A plan written to a non-blocking pipe that is full can be lost without an error (Python does not report it); the run starts anyway.
