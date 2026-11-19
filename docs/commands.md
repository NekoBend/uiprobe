# Commands

The `crawl` command has its own page: [Previewing a crawl](crawl.md). [Options](#options) and [environment variables](#environment-variables) are at the end of this page.

## Listing devices

List the devices the adb server knows:

```console
$ uiprobe devices
SERIAL            STATE         MODEL
emulator-5554     device        sdk_gphone64_x86_64
0123456789ABCDEF  unauthorized
```

## Dumping a screen

Print the screen that is showing on the device as a tree:

```console
$ uiprobe -s emulator-5554 dump
com.example.shop/.LoginActivity  (s-23512491bbaf)
e1 FrameLayout
├ e2 TextView "ようこそ"
├ e3 EditText id=email hint="メールアドレス" [tap]
└ e4 Button "ログイン" id=login [tap] [disabled]
  └ e5 ImageView id=icon desc="lock"
```

Add `--json` to get JSON instead of text (`uiprobe dump --json`).

### Short view with `--brief`

A real screen is mostly containers with no text, so the full tree can run to over a hundred lines. `uiprobe dump --brief` prints a shorter text view. It is opt-in: `uiprobe dump` without it prints the full tree as before.

A small screen of 11 elements (the full tree is 12 lines with its header line) shows in 6 lines:

```console
$ uiprobe -s emulator-5554 dump --brief
com.example.settings/.Settings  (s-06d03764c6a8)
e2 ImageButton [tap]
e3 TextView "Settings"
e4 ScrollView [scroll]
├ e5 LinearLayout labels="Network & internet Mobile, Wi-Fi, hotspot" [tap]
└ e9 LinearLayout labels="Battery 100%" [tap]
```

- The header line is kept. Of the elements, only these are kept: those with text, a content description or a hint, and those that carry a `[tap]` or `[scroll]` mark. Everything else is dropped, for example a container or an icon with no label that cannot be pressed. A pressable icon with no label is still listed, with its `[tap]`.
- The element numbers are those of the same capture, so `e5` is the same element in the full view and in the brief view of one capture. Numbers of dropped elements are missing, so there are gaps. Element numbers identify elements within one capture only and may change on the next dump; they are not selectors (`tap --id` takes a resource id).
- Indentation follows the parent and child relation among the kept elements only: an element is shown under its nearest kept ancestor, and a kept element with no kept ancestor starts at the left edge. The dropped elements in between add no indentation.
- A pressable (`[tap]`) element with no text, description or hint of its own is folded into one line when it has labeled descendants and none of them carries a mark such as `[tap]`, `[scroll]` or `[checked]`. Each descendant gives its text, else its content description, else its hint, taking the first of the three that is not blank once spaces at both ends are removed. These labels are joined by one space in document order and written as `labels="..."`. A descendant that gave a label is not listed on its own line; one that gave none (all its fields blank) stays listed if the brief view keeps it anyway (for example because it has a `[tap]` mark). A settings row with a title and a summary shows as one line this way. The fold does not go into a nested `[tap]` or `[scroll]` element: that element and its subtree keep their own labels, and a nested pressable element may fold its own labels the same way. A row is not folded when it has a label itself, when no descendant gives a label, or when a labeled descendant that the fold visits carries a mark; the row and that descendant are listed as usual.
- A folded label is at most 60 characters, counted before escaping. A longer label is cut and ends with `...`, which counts in the 60; spaces left at the cut are removed, so a cut label can be a little shorter than 60. Text is then escaped as in the full view, so a line break or control character shows as `\n` or `\u202e` and the printed value can be longer than 60 characters.
- `--brief` cannot be used with `--json`, because the JSON stays complete. The command exits with 2 (`usage_error`) and sends nothing to the device. `find`, `tap` and the other commands are not affected.

## Taking a screenshot

Save the screen that is showing on the device as a PNG file:

```console
$ uiprobe -s emulator-5554 screenshot -o login.png
saved login.png (1080x2400, 84213 bytes)
```

`-o PATH` is required and names a new file. The file is created and never replaced: if a file, a link (also one whose target is missing) or a folder exists at `PATH`, the command exits with 2 (a usage error) before it asks the device for anything, and the existing item is left untouched. The folder of `PATH` must exist. `PATH` is written as you typed it, in the output too; on Windows a path such as `shots\login.png` or `C:\shots\login.png` works as usual, but a colon after the drive (`login.png:extra`, which names a stream inside a file) is a usage error, because creating a stream changes the file it belongs to.

The file is then opened create-only, so an item that appears at `PATH` while the screenshot is taken is still left untouched (the same usage error). Two limits remain: on Windows a link made at `PATH` in the instant between the check and the creation is followed (as for `uiprobe init`), and on every system a link or junction in a folder above `PATH` decides where the file goes. Use a folder that other programs cannot change while the command runs.

The screenshot is taken with exactly this command, through the binary-safe `exec:` service, and the bytes are saved as they come:

```sh
screencap -p
```

With `--json` uiprobe also asks for `wm size`, after the screenshot and before the file is made, for the optional `display` field; when that cannot be read the field is left out and the command still succeeds.

The image is checked in memory before the file is made, without decoding it: it must start with the PNG signature; its first chunk must be a well-formed header (`IHDR`) with a width and a height above 0, a bit depth that its color type allows, and standard compression, filter and interlace methods; every chunk must have a length that fits in the data and a four-letter type; there must be no second header; a palette (`PLTE`, 1 to 256 colors) may appear once, before the image data, is required for a palette image and refused for a gray one; the image data chunks (`IDAT`) must form one unbroken run; and an empty closing `IEND` chunk must end the data. Checksums (except the fixed one of `IEND`), the order and contents of other chunks, and the pixels are not checked. Empty output, a cut-off image or data that fails these checks exits with 16 (`screenshot_failed`) and makes no file. An app can forbid screenshots of a screen, for example a private browser tab: on older Android (an emulator at API 24, for example) `screencap` then gives empty output, so the command fails, but `uiprobe dump` still returns the structure of that screen. On newer Android (API 36, for example) such a screen gives a mostly black image instead. That is a valid PNG, so it is saved like any other; look at the image before you rely on it.

If the file was created but writing it failed (a full disk, for example), the partial file stays where it is and the error (exit 1, `file_error`) names it; uiprobe never deletes it. If the file could not be created at all (the folder is missing, access is denied, or Windows refuses the name), the error names the path and no file exists. Ctrl+C while the file is written exits with 130 (`interrupted`) and names the file, which stays and may be incomplete. A Ctrl+C at another instant (inside the creation itself, right after it before writing starts, or after the file is complete while the result is printed) may only say "interrupted" and can leave a partial or a complete file.

With `--json` the result is a `uiprobe.screenshot/1` document, see [Output formats](output.md). A screenshot can contain personal data, like a dump; see [Safety](safety.md#personal-data-in-dumps).

## Finding elements

`uiprobe find` lists the elements of the current screen that match all the criteria you give. At least one is needed:

| Option | Matches |
| --- | --- |
| `--text TEXT` | The element's text. Spaces and line breaks at both ends are ignored on both sides, because some Android versions put a line break in front of a text. Otherwise the text must be equal. |
| `--id ID` | The resource id, equal to the full id (`com.example.shop:id/login`) or to the part after `:id/` (`login`). |
| `--desc DESC` | The content description, equal. |

```console
$ uiprobe find --text "ログイン"
1 element matches  (s-23512491bbaf)
e4 Button "ログイン" id=login [tap] [disabled] center=540,1860
```

Only visible elements count. The visible part of an element is its bounds cut to the display, and the element is visible only when that part is at least 8 pixels wide and at least 8 pixels high (`MIN_VISIBLE_PX`). An element whose width or height is zero, whose bounds are the wrong way round (as in `[80,623][203,616]`, seen on API 36), that lies entirely off the display, or of which only a thinner sliver shows (a list row with 2 pixels at the bottom edge, a WebView field scrolled out of view of which Android reports a thin strip) is not visible and never matches. `center` is the middle of the visible part, rounded down; it is the point a tap would use.

`find` still lists an element that is covered, or that has nothing to press at its `center`, but without a `center`, because `tap` refuses it. An element is covered when the topmost meaningful element (of any size) at its `center` is neither the element nor one of its descendants (see [Tapping](#tapping)).

Finding nothing is not an error: the command prints `0 elements match` followed by the screen id and exits with 0. With `--json` it prints a `uiprobe.find/1` object (see [Output formats](output.md)) with an empty `elements` list. At most 20 elements are listed; `total` in the JSON says how many matched.

If you do not know the exact words, `find_one` in [Python](python.md) suggests candidates when nothing or several elements match.

uiprobe uses the device given with `-s`, else the one named in `ANDROID_SERIAL`, else the only ready device. If no device is named and more than one is ready, uiprobe stops with an error and asks you to choose.

## Tapping

`uiprobe tap` presses one point of the screen that is showing: a tap, or with `--long` a long press. Name the element with `--text`, `--id` and `--desc` (the same options and the same rules as `find`; they combine with AND), or give a point with `--xy X Y`. `--xy` cannot be combined with the other three, and one way or the other is required; `X` and `Y` are plain decimal integers from 0 to 32767.

By default a tap holds still for 10 ms: it is sent as a swipe from the point to itself, so the point never moves. A plain Android `input tap` is barely visible in the "Pointer location" developer option, while a 10 ms hold is (measured on one device only, a Galaxy S25 Ultra at 60 Hz: 10 ms was visible, 6 ms was not). `--press-ms N` sets the hold, an integer from 0 to 200; `--press-ms 0` sends the old plain `input tap X Y`. `--press-ms` cannot be combined with `--long`, which keeps its fixed 800 ms. The upper bound stays below Android's usual default long-press time (about 400 to 500 ms, and longer when accessibility settings raise it), but an app may use its own shorter threshold; use `--long` when a long press is wanted. `--press-ms` may be repeated: every value is checked (one invalid value is a usage error with exit code 2 and nothing is sent) and the last one is used. The point where the finger lands is decided exactly as before; only how the press is sent changed. uiprobe adds no movement to the press.

```console
$ uiprobe tap --id email
tapped e3 EditText id=email hint="メールアドレス" [tap] at 540,660
$ uiprobe tap --id icon --desc "lock" --long
long-tapped e5 ImageView id=icon desc="lock" at 120,1840
$ uiprobe tap --xy 540 1860
tapped 540,1860
$ uiprobe tap --xy 540 1860 --press-ms 0
tapped 540,1860
```

- With a selector, uiprobe dumps the screen, takes the one visible element (at least 8 x 8 pixels on the display) that matches, and presses the center of its visible part, rounded down. That is the `center` that `find` prints.
- `tap` and `swipe --direction` with a selector read only what their checks need: the screen elements and the display size. They skip the activity and API level reads that `dump` and `find` make; `find` keeps the activity because its screen id includes it.
- Before it presses, uiprobe checks what is on top at that point. The topmost meaningful element whose part on the display holds the point must be the element itself or one of its descendants (a label inside a button is fine); the topmost one may be the selected element or one of its descendants. Any meaningful element on top counts, whatever its size, even a strip one pixel thin, because it would receive the touch; the 8-pixel minimum applies to the element being pressed, not to what covers it. Meaningful means: it has the clickable flag, or a text, content description or hint that is not empty after whitespace is stripped. Topmost means the last such element in document order, because later elements are drawn on top. An empty layout container on top is looked through. When another element is on top, nothing is pressed: uiprobe exits with 12 (`element_not_visible`), the message names the element that covers it, and with `--json` the `details` hold `element`, `bounds`, `reason`, `covered_by` (the id of the element on top) and `point`. An element that is not meaningful itself (an unlabeled icon) inside a clickable row is refused too: the row is the topmost meaningful element, so it would receive the press, and the message says so (`covered_by` is the row). This holds also for such an element selected by its resource id: press the row instead. When no meaningful element holds the point at all (for example an isolated element that has only a resource id), nothing there can receive a press, so the element is refused too, with the reason `no_target` (`details`: `element`, `bounds`, `reason` and `point`; the message says that nothing that can receive a press is at that point). A selector counts every element that matches and has a non-empty part on the display, whatever its size: with two or more matches `tap` exits with 13 (`multiple_elements`), even when only one of them is 8 x 8 pixels or more, and nothing is sent. When the only match shows under 8 x 8 pixels, `tap` exits with 12 (`element_not_visible`, reason `too_small`, plus `visible_size`); `uiprobe find` lists only the elements of 8 x 8 pixels or more. `no_visible_part` is a reason for the library (`Screen.tap_point`, `Device.tap`).
- When nothing matches, or several do, nothing is pressed. uiprobe exits with 12 (`element_not_found`) or 13 (`multiple_elements`). Text mode prints only the error message; with `--json` the `details` of the `uiprobe.error/1` object hold `criteria`, `total` and `candidates` (as `find_one` does in Python).
- The cover check uses the dump. If the screen changes before Android receives the input, a different control can receive the tap even when the command reports success.
- A disabled element (the `[disabled]` tag) is still pressed; uiprobe prints a one-line warning on stderr, never on stdout.
- With `--xy` there is no dump and no check: the point is pressed as given, because you named the point.
- `tap` acts on the screen showing now. The foreground app is not checked, and there is no deny-word guard: it presses what you name, including a button called "Delete".
- With `--json` the result is a `uiprobe.action/1` object (see [Output formats](output.md)); `--press-ms` does not change it (`action` is `tap` or `long_tap`).

The commands sent to the device are exactly these, with the numbers filled in:

```sh
input swipe X Y X Y 10; echo uiprobe-exit:$?
input tap X Y; echo uiprobe-exit:$?
input swipe X Y X Y 800; echo uiprobe-exit:$?
```

The first is the default tap (`10` is the `--press-ms` value), the second is `--press-ms 0`, and the third is `--long`: a swipe from a point to itself that lasts 800 ms. The `echo` reads the exit status of `input`, which the adb `shell:` service does not return. On Android 7 to 16 (API 24 to 36) that status is 0 even for an `input` subcommand that does not exist, so uiprobe counts a press as done only when the line `uiprobe-exit:0` is the last line of the output and no other line is non-empty (empty lines are ignored). Anything else is an `input_failed` error with exit code 14, whose details say why: `no_exit_status`, `exit_status` or `unexpected_output`.

uiprobe sends each press once and never resends it, not even after a lost connection, because a resend could press twice. Exit 14 does not prove that nothing was pressed: `input` may have acted before it failed. Look at the screen before you repeat the command.

## Swiping and scrolling

`uiprobe swipe` drags one finger across the screen that is showing. Give both ends with `--from X Y --to X Y`, or scroll with `--direction up|down|left|right`, where the direction is the way the finger moves (`up` shows content further down). With `--direction` you can limit the scroll to one element with `--text`, `--id` and `--desc` (the same rules as `find` and `tap`; they combine with AND); without them the whole display is used, and uiprobe reads only the display size (`dumpsys window displays`, one request) instead of dumping the screen, falling back to a dump of the screen elements and `wm size` (without the activity and API level reads) when that size cannot be read. A selector limits the swipe coordinates to the element; it does not check whether something covers the element, so an overlay can receive the gesture. `X` and `Y` are plain decimal integers from 0 to 32767. `--ms N` sets the duration in milliseconds: a plain decimal integer from 1 to 10000, 300 when not given.

```console
$ uiprobe swipe --direction up
swiped 160,511 -> 160,128 in 300 ms
$ uiprobe swipe --direction left --id pager --ms 600
swiped 255,320 -> 64,320 in 600 ms on e4 ViewPager id=pager
$ uiprobe swipe --from 540 1800 --to 540 600
swiped 540,1800 -> 540,600 in 300 ms
```

- The usage errors (exit 2, before the adb server is contacted) are: `--from` without `--to` or the reverse; `--from` or `--to` together with `--direction`; `--text`, `--id` or `--desc` without `--direction`; neither `--from` nor `--direction`; a direction other than the four; a bad `--ms`; and `--from` equal to `--to`. A swipe that does not move is a press and is refused; use `tap`.
- With `--from` and `--to` there is no dump and no check: the swipe is sent as given.
- With `--direction` and a selector (`--text`, `--id` or `--desc`), uiprobe dumps the screen and takes the one element that matches; matches are counted over every element with a non-empty part on the display, whatever its size (exit 12 `element_not_found` when there is none, 13 `multiple_elements` when there are several, 12 `element_not_visible` with reason `too_small` when the only match shows under 8 x 8 pixels, and nothing is sent). A disabled element is still scrolled, with a one-line warning on stderr only. With `--direction` and no selector, uiprobe does not dump: it reads only the display size, and dumps the screen instead when that size cannot be read.
- The scroll geometry: the area is the display, or the visible part of the element. It is shrunk by 20% of its width on the left and on the right and by 20% of its height at the top and at the bottom, then kept at least 5% of the display width away from the left and right display edges and 5% of the display height away from the top and bottom ones, so the gesture does not start in the zones of the back, notification and home gestures. All shares are rounded down. The rectangle is half-open like element bounds (its right and bottom edges are exclusive), so the finger runs from the first to the last pixel of it, along its middle. With the rectangle `left, top, right, bottom`: `up` goes from `(center_x, bottom - 1)` to `(center_x, top)`, `down` is the reverse, `left` goes from `(right - 1, center_y)` to `(left, center_y)`, `right` is the reverse (centers are rounded down). Every end point is therefore inside the rectangle, also in a very narrow element. When the rectangle is under 2 pixels wide or high, or a display side is under 20 pixels, nothing is sent and the command fails with a usage error ("the area is too small to scroll"); this is decided before any input is sent, in both paths (after the dump with a selector, after reading the display size without one), so it can depend on the screen, not only on the arguments, and the exit code is still 2. When `wm size` could not be read during the dump, the display size is only a guess from the windows, so nothing is sent and the command fails with a usage error (exit 2, "the display size is unknown ..."); use explicit points with `--from` and `--to`.
- `swipe` acts on the screen showing now. The foreground app is not checked, and there is no deny-word guard. A swipe that starts at a screen edge can trigger a system gesture.
- With `--json` the result is a `uiprobe.action/1` object (see [Output formats](output.md)).

The command sent to the device is exactly this, with the numbers filled in (a scroll sends the same form):

```sh
input swipe X1 Y1 X2 Y2 MS; echo uiprobe-exit:$?
```

The exit status is read as for `tap`. uiprobe sends the swipe once and never resends it, not even after a lost connection. The read timeout of that one call is the duration in seconds plus 10 seconds. Exit 14 (`input_failed`) does not prove that nothing happened: the gesture may have been performed before `input` failed. Look at the screen before you repeat the command.

## Pressing keys

`uiprobe key NAME` presses one key. `NAME` is an alias (any case) or a full key code name:

| Alias | Key code |
| --- | --- |
| `back` | `KEYCODE_BACK` |
| `home` | `KEYCODE_HOME` |
| `enter` | `KEYCODE_ENTER` |
| `delete` | `KEYCODE_DEL` |
| `forward_delete` | `KEYCODE_FORWARD_DEL` |
| `tab` | `KEYCODE_TAB` |
| `escape` | `KEYCODE_ESCAPE` |
| `menu` | `KEYCODE_MENU` |
| `search` | `KEYCODE_SEARCH` |
| `recents` | `KEYCODE_APP_SWITCH` |
| `up`, `down`, `left`, `right` | `KEYCODE_DPAD_UP`, `KEYCODE_DPAD_DOWN`, `KEYCODE_DPAD_LEFT`, `KEYCODE_DPAD_RIGHT` |
| `center` | `KEYCODE_DPAD_CENTER` |
| `page_up`, `page_down` | `KEYCODE_PAGE_UP`, `KEYCODE_PAGE_DOWN` |
| `space` | `KEYCODE_SPACE` |

```console
$ uiprobe key back
pressed key KEYCODE_BACK
$ uiprobe key KEYCODE_F1
pressed key KEYCODE_F1
```

A full name must match `KEYCODE_[A-Z0-9_]{1,40}` exactly and is sent as given; `keycode_back` is not an alias and is refused. Bare numbers such as `4` are refused: on the devices measured, `input keyevent 0` typed the character 0 into a focused field, so `input keyevent` does not reliably read a number as a key code. Use a `KEYCODE_` name. A name made only of two or more digits after `KEYCODE_`, such as `KEYCODE_26`, is refused too, because Android may read it as a number; `KEYCODE_0` to `KEYCODE_9` are the digit keys and are accepted. Any other name is a usage error (exit 2) before the adb server is contacted.

Keys that act on the whole device, not on the app in front, are refused with a usage error (exit 2) before the adb server is contacted, whether given as an alias or as a name. The refused keys come from the settings file (see [Settings file](settings.md)) when one is found: `uiprobe key` reads `--config PATH`, else `./uiprobe.toml` when that file exists, and otherwise uses the built-in lists below. The default `./uiprobe.toml` does not have to exist (`key` is a named command, unlike `crawl`), but a file named with `--config` must: a missing one is a usage error, as is a file that cannot be read or is invalid, or a `--config` value that ends with a path separator. With a file, the refused keys are its `[keys] refuse` and `refuse_prefixes` (minus `remove`), plus the fixed keys, which are always refused whatever the file says (`KEYCODE_POWER`, `KEYCODE_SLEEP`, `KEYCODE_SOFT_SLEEP`, `KEYCODE_LOCK`, `KEYCODE_CALL`, `KEYCODE_ENDCALL`, `KEYCODE_HEADSETHOOK`, `KEYCODE_TV_POWER`, `KEYCODE_AVR_POWER`, `KEYCODE_STB_POWER`). Settings warnings go to stderr. Only the fixed keys are unconditional. The built-in groups below combine the fixed keys with defaults; the generated file lists the defaults, not the fixed keys, and its `[keys]` table can change them: list a key under `remove` to send it (a key that a remaining prefix such as `KEYCODE_MEDIA_` still covers stays refused, with a warning, until that prefix is removed too), or add one such as `KEYCODE_HOME` to refuse it. The built-in groups are:

- Power and sleep: `KEYCODE_POWER`, `KEYCODE_SLEEP`, `KEYCODE_WAKEUP`, `KEYCODE_SOFT_SLEEP`, `KEYCODE_TV_POWER`, `KEYCODE_AVR_POWER`, `KEYCODE_STB_POWER`.
- Calls: `KEYCODE_CALL`, `KEYCODE_ENDCALL`, `KEYCODE_HEADSETHOOK`.
- Sound and media playback: `KEYCODE_VOLUME_UP`, `KEYCODE_VOLUME_DOWN`, `KEYCODE_VOLUME_MUTE`, `KEYCODE_MUTE` and every name starting with `KEYCODE_MEDIA_`.
- Screen: `KEYCODE_BRIGHTNESS_UP`, `KEYCODE_BRIGHTNESS_DOWN`, `KEYCODE_SYSRQ`, `KEYCODE_SCREENSHOT`.
- System UI and assistants: `KEYCODE_NOTIFICATION`, `KEYCODE_ASSIST`, `KEYCODE_VOICE_ASSIST`, `KEYCODE_ALL_APPS`, `KEYCODE_SETTINGS`, `KEYCODE_LANGUAGE_SWITCH`, `KEYCODE_MANNER_MODE`, `KEYCODE_PAIRING`, `KEYCODE_LOCK`, `KEYCODE_EMOJI_PICKER`, `KEYCODE_KEYBOARD_BACKLIGHT_DOWN`, `KEYCODE_KEYBOARD_BACKLIGHT_UP`, `KEYCODE_KEYBOARD_BACKLIGHT_TOGGLE`, `KEYCODE_RECENT_APPS`, `KEYCODE_PROFILE_SWITCH`, `KEYCODE_DO_NOT_DISTURB`, `KEYCODE_META_LEFT`, `KEYCODE_META_RIGHT`, `KEYCODE_DICTATE`, `KEYCODE_ACCESSIBILITY`, `KEYCODE_CONTEXTUAL_SEARCH`, `KEYCODE_CONTEXTUAL_QUERY`, `KEYCODE_CONTEXTUAL_INSERT` and every name starting with `KEYCODE_SYSTEM_NAVIGATION_`.
- Keys that open other apps or switch inputs: `KEYCODE_CAMERA`, `KEYCODE_FOCUS`, `KEYCODE_EXPLORER`, `KEYCODE_ENVELOPE`, `KEYCODE_CONTACTS`, `KEYCODE_CALENDAR`, `KEYCODE_MUSIC`, `KEYCODE_CALCULATOR`, `KEYCODE_BOOKMARK`, `KEYCODE_GUIDE`, `KEYCODE_DVR`, `KEYCODE_TV`, `KEYCODE_TV_INPUT`, `KEYCODE_WINDOW`, `KEYCODE_STYLUS_BUTTON_TAIL`, `KEYCODE_AVR_INPUT`, `KEYCODE_STB_INPUT` and every name starting with `KEYCODE_TV_` (television controls), `KEYCODE_VIDEO_APP_`, `KEYCODE_FEATURED_APP_`, `KEYCODE_DEMO_APP_` (apps assigned on a TV device) or `KEYCODE_STEM_` (watch buttons).

The reason is that they act outside the app: they switch the device off, place calls, change the sound, media playback or brightness, take screenshots, start assistants or open other apps. The names follow Android's `KeyEvent` constants. The list covers the key names above, which include some that exist only on newer Android versions; a name that does not exist on a device is still refused. By default the navigation keys of the app session stay allowed (nothing in the built-in lists or the generated file refuses them; adding one, for example `KEYCODE_BACK`, to `[keys] refuse` makes `uiprobe key back` a usage error): back, home, recents (`KEYCODE_APP_SWITCH`), menu, search, enter, delete, tab, escape, space, the direction and page keys, letters and digits.

Not every key in the power and sleep group is a fixed key of the [settings file](settings.md): the fixed keys, which a settings file can never remove, are listed there, and `KEYCODE_WAKEUP` is not one of them (`KEYCODE_LOCK` is).

`key` acts on the screen showing now. The foreground app is not checked, and there is no deny-word guard. The command sent to the device is exactly this:

```sh
input keyevent KEYCODE_BACK; echo uiprobe-exit:$?
```

It is sent once and never resent. Exit 14 (`input_failed`) does not prove that no key was pressed. With `--json` the result is `{"schema": "uiprobe.action/1", "action": "key", "key": "KEYCODE_BACK"}`.

## Creating the settings file

`uiprobe init [PATH]` creates the settings file `uiprobe.toml` (at `./uiprobe.toml` unless you give a path) and prints its path. It needs no device and no adb server, and it never overwrites a file. Details are in [Settings file](settings.md).

## Options

These options work before or after the command, except for `init`, which accepts only `--json` from this table (see [Settings file](settings.md)).

| Option | Meaning |
| --- | --- |
| `-s SERIAL`, `--serial SERIAL` | The device to use. May be given once. Without it, the device named in `ANDROID_SERIAL` is used, else the only ready device. `uiprobe devices` lists every device, also when `-s` is given. |
| `--adb PATH` | The adb executable to run (it is run only to start the adb server). |
| `--adb-server tcp:HOST:PORT` | The adb server to talk to. `HOST` is an IP address or `localhost` (which means `127.0.0.1`); IPv6 addresses are written in brackets, `tcp:[::1]:5037`. A scoped IPv6 address such as `tcp:[fe80::1%eth0]:5037` is accepted. Host names are not accepted, because uiprobe never makes a DNS lookup: if you use a host name with adb, write the IP address for uiprobe instead. |
| `--no-start-server` | Never start the adb server; fail if it is not running. |
| `--json` | Print JSON. Errors are printed as JSON on stdout too. |

`uiprobe --version` prints one line of plain text to stdout, such as `uiprobe 0.1.0a1.dev0`, also with `--json`, and exits without contacting the adb server. It works only before a command. All development snapshots of one version print the same line, so when you report a problem, also name the commit you installed.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `ANDROID_SERIAL` | The device to use when `-s` is not given. |
| `ADB_SERVER_SOCKET` | The adb server address, `tcp:HOST:PORT`, with the same `HOST` rules as `--adb-server`. |
| `ANDROID_ADB_SERVER_PORT` | The port of the adb server on `127.0.0.1`. |
| `UIPROBE_ADB` | Path of the adb executable. |
| `ANDROID_HOME`, `ANDROID_SDK_ROOT` | SDK directories; `platform-tools/adb` (`platform-tools/adb.exe` on Windows) below them is used when adb is not found otherwise. |

The adb server address is taken from `--adb-server`, then `ADB_SERVER_SOCKET`, then `ANDROID_ADB_SERVER_PORT`, then `tcp:127.0.0.1:5037`. The adb executable is taken from `--adb`, then `UIPROBE_ADB`, then `adb` on `PATH`, then the SDK directories. Empty variables count as unset.

uiprobe starts the adb server (`adb start-server`, once) only when the server is not running, you did not choose an address (by option or environment variable), `--no-start-server` was not given, `ANDROID_ADB_SERVER_ADDRESS` is not set to a non-empty value, and adb can be found. uiprobe does not read `ANDROID_ADB_SERVER_ADDRESS` as an address; while it has a non-empty value, uiprobe does not start a server (an empty value counts as unset), so give the address as `ADB_SERVER_SOCKET=tcp:IP:PORT` or `--adb-server`.
