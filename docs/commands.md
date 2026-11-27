# Commands

The `crawl` command has its own page: [Crawling an app](crawl.md). [Options](#options) and [environment variables](#environment-variables) are at the end of this page.

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

`dump` prints to the terminal and is not a run folder: it keeps every text, the text of password fields included. The same holds for the text a dry run (`uiprobe crawl PACKAGE`) prints. Only the records of a pressing crawl hide texts (see [What is recorded of a screen's texts](run-folder.md#what-is-recorded-of-a-screens-texts)).

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

`-o PATH` is required and names a new file. The file is created and never replaced: if a file, a link (also one whose target is missing) or a folder exists at `PATH`, the command exits with 2 (a usage error) before it asks the device for anything, and the existing item is left untouched. The folder of `PATH` must exist. `PATH` is written as you typed it, in the output too; on Windows a path such as `shots\login.png` or `C:\shots\login.png` works as usual, but a colon after the drive (`login.png:extra`, which names a stream inside a file) is a usage error, because creating a stream changes the file it belongs to (a path in the `\\?\GLOBALROOT\GLOBAL??\C:\...` form is refused too; give it with its drive letter). On Linux or macOS a colon is an ordinary character and is not refused; an NTFS drive mounted there with its streams turned on (NTFS-3G with `streams_interface=windows`, for example) still reads it as a stream, so do not write to such a drive. If uiprobe cannot tell whether something is at `PATH` (access to a folder above it is denied, for example), the command exits with 1 (`file_error`, `details.created` false) before it asks the device, and creates nothing.

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

Keys that act on the whole device, not on the app in front, are refused with a usage error (exit 2) before the adb server is contacted, whether given as an alias or as a name. The refused keys come from the settings file (see [Settings file](settings.md)) when one is found: `uiprobe key` reads `--config PATH`, else `./uiprobe.toml` when that file exists, and otherwise uses the built-in lists below. The default `./uiprobe.toml` does not have to exist (`key` is a named command, unlike `crawl`), but a file named with `--config` must: a missing one is a usage error, as is a file that cannot be read or is invalid, or a `--config` value that ends with a path separator or `/.` (the folder form of a path; the message names the file to give instead). With a file, the refused keys are its `[keys] refuse` and `refuse_prefixes` (minus `remove`), plus the fixed keys, which are always refused whatever the file says (`KEYCODE_POWER`, `KEYCODE_SLEEP`, `KEYCODE_SOFT_SLEEP`, `KEYCODE_LOCK`, `KEYCODE_CALL`, `KEYCODE_ENDCALL`, `KEYCODE_HEADSETHOOK`, `KEYCODE_TV_POWER`, `KEYCODE_AVR_POWER`, `KEYCODE_STB_POWER`). Settings warnings go to stderr. Only the fixed keys are unconditional. The built-in groups below combine the fixed keys with defaults; the generated file lists the defaults, not the fixed keys, and its `[keys]` table can change them: list a key under `remove` to send it (a key that a remaining prefix such as `KEYCODE_MEDIA_` still covers stays refused, with a warning, until that prefix is removed too), or add one such as `KEYCODE_HOME` to refuse it. The built-in groups are:

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

## Typing text

`uiprobe type TEXT` types text into the field that has focus, in whatever app is in front.

```console
$ uiprobe type "hello world"
typed 11 characters
```

Only printable ASCII is typed: the characters from space (0x20) to `~` (0x7E). Before anything is sent, these are refused with a usage error (exit 2) and no request goes to the adb server:

- a character outside ASCII, such as `é` (non-ASCII text failed on every Android version tried: emulators at API 24, 30 and 36). The message names the first one: `character U+00E9 ('\xe9') at position 3 is not ASCII; this version types ASCII text only`. With `--json` the error `details` hold `character` (`U+00E9`), `escaped` (`'\xe9'`) and `position` (0-based, `3`).
- a newline or tab, which can act as Enter or move the focus. To press Enter use `uiprobe key enter`.
- any other control character (0x00 to 0x1F and 0x7F).
- empty text.

Give the text as one quoted argument, and put `--` before a text that starts with `-` (as in `uiprobe type -- -5`; some such texts are read as options without it). The help of the command is `uiprobe type --help`; `-h` is not an option of `type`, so a text holding it is not taken for a help request.

Unlike the other commands, `type` takes the device options `-s`/`--serial`, `--adb` and `--adb-server` only before the command, as in `uiprobe -s SERIAL type TEXT`. After `type` they are refused with a usage error (exit 2) before anything else happens, in every spelling argparse could read (`-sVALUE`, `--serial=VALUE`, and shortened names such as `--ser`), so that no word of an unquoted text is ever used as a serial or an address. The message does not show the value. After `--` everything is text: `uiprobe -s SERIAL type -- -s` types `-s`. `--json` and `--no-start-server`, which take no value, can still follow `type`.

Words of an unquoted text can be read as arguments, so when the command is `type` uiprobe hides them in its errors:

- When argparse cannot read the arguments (an unquoted text of two words, a text starting with `-` without `--`), the usage error shows none of them and points to `uiprobe type --help`.
- A refused character is named by its code point, escaped form and position (see above), so that one character of the text does appear in the message.

This hiding is best effort: how a shell splits an unquoted text, and how argparse reads the words, can still bring part of the text into an error in a case not listed here. The reliable way is to quote the text as one argument, with `--` before it when it starts with `-`, or to pass it from Python with `Device.type_text`, which takes the text as a value and never parses it.

The text is sent as `input text` with the text in single quotes for the device shell, so spaces, quotes, `;`, `&&`, `|`, `$( )`, backticks, `<`, `>` and `\` arrive as typed. The device turns the two characters `%s` into a space, so the text is split between `%` and `s` and sent as separate commands; a `%` alone is typed as it is. Long text is split into pieces of at most 256 characters, sent one after the other. The command for each piece is exactly this, for example for `it's 50%`:

```sh
input text 'it'"'"'s 50%'; echo uiprobe-exit:$?
```

Each piece is sent once and must report exit status 0 and print nothing else; otherwise typing stops with exit 14 (`input_failed`, `details.reason` as for `tap`: `exit_status`, `no_exit_status` or `unexpected_output`). Empty lines are ignored. The exit status itself is not shown, because the device prints that line and a reply could repeat the text in it. An adb error while sending (a lost connection, an offline device) stops typing too, with its own exit code. In both cases `details` hold `piece` (the piece typing stopped at, from 1), `pieces` (how many there are) and `pieces_done` (how many earlier pieces reported success); the message and `details` hold only fixed words and these numbers, never the text and nothing the device or adb answered, as that can repeat the text. The pieces before `piece` were probably typed, and `piece` itself may have been typed in part or in full, so running the command again can type part of the text twice: look at the screen before you repeat it. Exit status 0 does not prove that the text arrived (it can be 0 even when nothing was typed), and the result is not read back. An empty field shows its hint as text in a dump, so a dump cannot tell the hint from typed text. Look at the screen, or dump it, to check.

With `--json` the result is `{"schema": "uiprobe.action/1", "action": "type", "length": 11, "device": {"serial": ...}, "captured_at": ...}`. `length` is the number of characters of the text that was given, all of which were sent with exit status 0. The text itself is never written to the output, because it can be a password. It is still visible elsewhere: on this computer, `TEXT` is an argument of the `uiprobe` process, so tools that list processes and the shell history can show it; on the device, each piece is an argument of the `input text` command while it runs, so a tool that lists the device's processes at that moment may show it (not measured). Do not type a secret you would not show there.

## Launching an app

`uiprobe launch PACKAGE` starts an app through its launcher activity and waits until it is the app in front. `PACKAGE` follows the Android package rule: two or more dot-separated segments, each an ASCII letter followed by ASCII letters, digits or underscores, at most 255 characters. Anything else is a usage error (exit 2) before the adb server is contacted, so a name can never carry shell syntax to the device.

```console
$ uiprobe launch com.example.shop
launched com.example.shop
$ uiprobe launch com.example.shop --restart --timeout 20
launched com.example.shop
```

The previous version of uiprobe started apps with `monkey -p PACKAGE -c android.intent.category.LAUNCHER 1`. That turned the device's auto-rotate setting on (the system setting `accelerometer_rotation` went from 0 to 1), seen on API 24 and API 36 emulators and on a Samsung API 36 phone. uiprobe now starts apps with `am start`, which does not change it: on an API 36 emulator, the system, global and secure settings read before and after `uiprobe launch`, `uiprobe launch --restart` and a short `crawl --press` showed no setting whose value changed (auto-rotate stayed off; only new keys appeared, as they also do when nothing is sent). `monkey` is never sent, and there is no fallback to it. The commands below go through the `shell:` service, each once and never resent.

First uiprobe reads the app's launcher activity (this changes nothing on the device):

```sh
cmd package resolve-activity --brief -c android.intent.category.LAUNCHER PACKAGE; echo uiprobe-exit:$?
```

Its output must be zero or more lines of the form `priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true`, then exactly one line `PACKAGE/CLASS`, with exactly `PACKAGE` before the slash and a class made of dot-separated parts of ASCII letters, digits, `_` and `$` (it may start with `.`, at most 255 characters), and then the line `uiprobe-exit:0`, last. Anything else stops the launch before anything is sent that changes the device (`launcher_unreadable`): `No activity found` for a package that is not installed or has no launcher activity, another exit status, or output that ends early (without the last line, a component cut off at the end of the output is never taken).

With `--restart` the app is then stopped with `am force-stop PACKAGE; echo uiprobe-exit:$?`, so the launch starts a fresh task. The app loses any state it has not saved. The stop must report exit status 0 and print nothing else, empty lines aside (on the measured API 24 and API 36 emulators a stop prints nothing, also for an app that is not running or not installed); otherwise the start is not sent. Then the launcher activity is read again, and the start uses that second read, since the launcher can change while the app stops (as with `monkey`, which picked it after the stop). When that second read fails, the start is not sent: the exit is 15 with `launcher_unreadable` and `details.force_stopped` `true` (the app was stopped and not started). Any other error of that read (a device error such as a timeout) keeps its own code and also carries `details.force_stopped` `true`. Without `--restart` an app that is already running comes forward where it was.

Then the app is started, with `PACKAGE/CLASS` as read:

```sh
am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n PACKAGE/CLASS; echo uiprobe-exit:$?
```

The start counts as successful only when all of this holds (empty lines are ignored, and one CR at the end of each line is cut):

- No line holds a control character, and no line starts with `Error` (API 24 prints `Error type 3` and `Error: Activity class {...} does not exist.` with exit status 0, so the status alone proves nothing).
- The last line is `uiprobe-exit:0`.
- Exactly one line `Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] cmp=PACKAGE/CLASS }` names the component that was sent (in its short `.Name` or its full spelling).
- Exactly one line `Status: ok`.
- Exactly one line `Activity: PACKAGE/CLASS` for the same package (a reply that names another package is waited on below, and fails with `other_activity` only when the app does not come to the front). The class may differ: Settings, started as `.Settings`, reports `.homepage.SettingsHomepageActivity` on a Samsung API 36 phone and an API 36 emulator.
- Any other line is one of these, each at most once: one of the two warnings seen when the app was already running (`Warning: Activity not started, intent has been delivered to currently running top-most instance.` and `Warning: Activity not started, its current task has been brought to the front`), `LaunchState: COLD`, `LaunchState: WARM`, `LaunchState: HOT` or `LaunchState: UNKNOWN (0)` (the values seen; another one fails the start with `unexpected_output`, a message that says the LaunchState value is unknown, and the line in `details.output`), `ThisTime: N`, `TotalTime: N`, `WaitTime: N` and `Complete`.

`-W` makes `am start` wait until the launch is done; its reply may stay silent for `--timeout` seconds (at least 1 second), else the launch fails with `start_timeout`, and whether the app started is unknown.

Then uiprobe reads the foreground app (as `Device.foreground` does) about every 0.5 seconds, at least once, until it equals `PACKAGE`; `--timeout SECONDS` (a plain decimal number from 0 to 600 of at most 32 characters, default 10) limits the wait. The timeout is the latest time a read may start, not a limit on the whole wait: a read that started in time still counts, so the wait can run past the timeout by the time of that read, and by how late a pause ends when the system wakes it late. This check is made even when `am start` reported success.

Each request selects the device by its serial, as for every other command. A launch never starts an adb server: it uses only the server that is there (`adb_server_unavailable` with `details.start_skipped` `in_launch` when it is gone; start it and launch again). After its first request, a launch never goes on after a failure it sees: a request that fails (also one whose failure a read of the app in front hides) ends it at once (`adb_error` with `details.reason` `launch_interrupted`); nothing more is sent. Accepted limit: if the device behind the serial is replaced between the launcher read and the start, the start uses the component read just before; it fails, or opens that component of the same package on the new device.

Limit: the `LaunchState:` values seen and accepted are `COLD`, `WARM`, `HOT` and `UNKNOWN (0)`. With any other value the start is not taken as a success: the launch fails with `unexpected_output` (in a crawl the run ends with `error`), although the app may have started.

`launch` starts the app on whatever device is selected and does not check what was in front before. It exits with 15 (`launch_failed`) when it cannot be sure the app is in front; `details.reason` says why:

| `details.reason` | Meaning |
| --- | --- |
| `launcher_unreadable` | The launcher activity could not be read on this device: the package is not installed, has no launcher activity, the read did not end with exit status 0, or the output had another shape. `details.output` is the first line that is not blank, stripped of spaces and tabs, cut to 200 characters and escaped. Nothing was stopped or started, except with `details.force_stopped` `true`: with `--restart` the read after the stop failed, so the app was stopped and not started. |
| `stop_failed` | With `--restart`, `am force-stop` reported an exit status other than 0 (`details.exit_status`), no exit status, or printed something (`details.output`, escaped); each is set only when known. The start was not sent. |
| `start_failed` | `am start` printed a line starting with `Error`, or reported an exit status other than 0 (`details.exit_status`). `details.output` is the `Error:` line (else the first `Error` line, else the first line that is not blank), cut to 200 characters and escaped. |
| `other_activity` | `am start` reported success, but its `Activity:` line names an activity of another app (`details.activity`, escaped; `details.output` is that line). Seen on an API 36 emulator: after a tap on the Settings search bar opened a screen of `com.google.android.settings.intelligence` and Settings was force-stopped, starting Settings brought that screen back. The system's own activities count too (owner `android`, such as the chooser `android/com.android.internal.app.ResolverActivity`). The app is waited on as after any start, and this is reported only when it is still not in front after `--timeout`. |
| `unexpected_output` | `am start` did not report the success described above; `details.output` is the line at fault, or else its first line that is not blank, cut to 200 characters and escaped. The app may still have started. |
| `start_timeout` | The reply of `am start -W` did not come: it stayed silent for `details.timeout` seconds. Whether the app started is unknown. |
| `not_in_front` | The start reported success, but the app was not in front within the timeout; `details.foreground` is the last package seen, escaped and, past 1000 characters, cut and ended with `...` (`null` when unknown) and `details.timeout` the limit. The app may still have started. |

With `--json` the result is a `uiprobe.action/1` object (see [Output formats](output.md)); `restart` is always written.

`uiprobe launch` stops the app only with `--restart`. A pressing crawl has the opposite default: `uiprobe crawl PACKAGE --press --out DIR` (or `uiprobe crawl PACKAGE --out DIR` with `press = true` in the settings file) force-stops the app and then starts its launcher activity; which screen the app then shows is up to the app (as with `other_activity` above: Settings on API 36 and 37 emulators brought back another package's search screen after a force-stop). `--no-restart` turns the force-stop off, so the crawl starts from the screen the app shows now (see [Pressing crawl](crawl.md#pressing-crawl---press)). `--no-restart` is used only when the crawl presses (with `--press`, or with `press = true` in the settings file); given to a preview, it is a usage error.

## Diagnosing the tap window check

`uiprobe diagnose tap-window` tells why the crawl's tap window check (see [Crawling from Python](python.md#crawling-from-python-alpha)) cannot read a device, in a report you can share: by default it holds no text or number of the device's output at all, only what uiprobe's reader concluded and lengths. Use it when `uiprobe crawl --press` refuses taps with `cover_unknown` and the run folder's end line has `details.rule`: a rule identifies a refusal of the window list reader. Each end line has its own `details`; the crawl's note at the end of the run looks at the run's first `cover_unknown` only, and suggests this command when that one has a rule. In a run folder written by this version, a `cover_unknown` without `details.rule` was not made by the reader (older versions record no rule at all, so there it says nothing): either an earlier check of the gate refused (such as `rotation` from the gate's own dump, or `target_uid`), or the list read and the point check refused later (`no_window`, `portal`); this command can then rightly say `readable`.

```console
$ uiprobe -s SERIAL diagnose tap-window
uiprobe diagnose tap-window: this output contains no device serial, app or window names, coordinates, accounts or network values; it read: dumpsys input
result: not readable: line_format, rule unknown_flag at line 26 of the dispatcher part
the display's list: its Display section, 3 windows read, the check refused here
lines (number, kind, length):
  24 window_row 351
  25 transform 34
> 26 window_row 357
  27 other 35
  28 other 33
```

On a list the check reads, the result line says `result: readable`, and no lines are shown.

It sends exactly one command to the device, `dumpsys input` (besides the adb server's device listing that every command uses to choose the device; `--package`, below, adds one read before it), and runs the same checks on it as the crawl does right before a tap, up to the point: the window list of display 0, then the display's orientation in the `Viewport` lines. It presses nothing. The report has:

- `result`: `readable`, or the reason the crawl would record (`no_list`, `line_format`, `region_format` or `rotation`; one exception: without `--package` this command knows no app, so a valid `samsungFlags=0x1` row that the reader reaches in display 0's list gives `line_format` with rule `samsung_flags`, also where the crawl accepts it on the app's own window and may tap; in another display's list that value gives `samsung_fields`, as in the crawl), the rule that refused (see the table below) and the line it refused at, counted from 1 in its part: in the `dispatcher` part the line `Input Dispatcher State:` is line 1; in the `input_reader` part (the `Viewport` lines above it) the first line of the output is line 1; when there is not exactly one `Input Dispatcher State:` line, or the line comes after the dispatcher's part (the first line not more indented than its header ends it), the part is `output`, the whole output from its first line. Some rules have no one line to blame; then no line is named.
- `note`: one line right after the result line, only when the rule is `samsung_flags` (what it means for a crawl) or when the uid of `--package` could not be read (whatever the result). Without `--package` it reads `note: a crawl accepts samsungFlags=0x1 on the target app's own window; diagnose is not told an app (add --package PKG to test that app's windows)`. With `--package` it says instead that the window is not the given app's own window by the crawl's owner uid and title test, or, when the app's uid could not be read, that no window counted as its own and a crawl of it would refuse every tap with the tap window check on. With `--package` and a uid that could not be read, any other result has `note: the given app's uid could not be read (dumpsys package); no window counted as its own`. JSON has the same sentence (without `note: `) in `note`, and no `note` otherwise.
- `the display's list`: the window list the reader took as display 0's, if any: whether it is a `Display:` section or the one list of every display (API 24 to 28), whether the reader got to it (it reads other displays' lists first and stops at the first refusal; a list it started to read counts as reached, also when it refused at its first line), how many window lines it read into a window there, and whether the check refused in it. Other displays' lists are not described: a window name that holds line breaks could have made them.
- `lines`: the failing line (marked `>`) and up to two lines before and after it in the same part; when no one line is to blame, the reader's own headings instead (its header and the headings of the display's list), at most 40. Each line is shown as its number, kind and length. Counts and lengths stop at 9999 when the report is printed: past that the text shows `9999+` and JSON shows 9999 (the Python object keeps the exact numbers).

A line's kind is what the reader made of it, with its own anchored patterns, where it reads: `heading` (its header, and the display's `Display:`, `Windows:`, `logicalSize=` and the line its list ended at), `window_row` (a window line it read into a window, or the window line a rule about window lines refused at), `transform` and `matrix_row` (the lines after a window line it read, and the display's transform line), `viewport` (a `Viewport` line of display 0 it read for the orientation), `blank`, and `other` for every other line, whatever it holds.

So the default report is built only from a closed vocabulary: uiprobe's own fixed words (the notice, labels, reasons, rule names, part names, line kinds) and numbers uiprobe counted (line numbers, lengths, windows read). No field name, value, flag, row or display number, orientation, sign or other character of the device's output is in it; a test walks the text, the JSON and the Python object of every rule case, capture, hostile input and reviewer's input, and of mutated outputs, and finds nothing else. The counts, the kinds and which lines are shown are what the reader read, so they can differ between outputs the reader judges the same (a window name that holds line breaks can add a line the reader reads, or change a count); they say something about the device, as the lengths do (how long a window title's line is).

With `--show-device-text` the report also has, for each shown line, its indent and tokens, every list the reader found (with its place among the lists), and the rows' forms (whether names are quoted, which fields follow them). Those come from the device: field names and values (as types such as `<num>` or as words such as `true`), known flag names, unknown flag names that are upper-case words (as `<unknown_flag:NAME>`), row numbers, a `Viewport` line's orientation. Window names and app text can appear in them, with or without line breaks, and can hold a serial or personal data. In this mode the first line of the report (and `notice` in JSON) says so; read the report before you paste it anywhere. With it the failing line of the example reads:

```text
> 26 window_row 6 357: row:<num:1> name=<text:15> id=<num> displayId=<num> inputConfig=NOT_FOCUSABLE|TRUSTED_OVERLAY|SLIPPERY|<unknown_flag:SEM_FOO> alpha=<num> frame=<rect> globalScale=<num> applicationInfo.name=<blank> applicationInfo.token=<text:6> touchableRegion=<text:7> ownerPid=<num> ownerUid=<num> dispatchingTimeout=<ms> token=<hex> touchOcclusionMode=<word:15>
```

The rules, under the reason the crawl records with them (`details.rule` next to `details.unreadable` in the run folder, see [The run folder](run-folder.md#json-formats)):

| Reason | Rule | The check refused because |
| --- | --- | --- |
| `no_list` | `no_dispatcher` | No `Input Dispatcher State:` line. |
| `no_list` | `dispatcher_twice` | More than one `Input Dispatcher State:` line (the second is named). |
| `no_list` | `no_list` | Neither a `Windows:` list nor `Display:` sections under the header, or the display's section has no `Windows` line. |
| `no_list` | `lists_twice` | More than one `Windows` line where only one may be (the second is named). |
| `no_list` | `list_heading` | The `Windows` line says more than `Windows:`. |
| `no_list` | `list_and_sections` | Both a list directly under the header and `Display:` sections. |
| `no_list` | `display_twice` | Two `Display:` sections of the same number (`Display: 00` and `Display: 0`). |
| `no_list` | `display_missing` | No `Display:` section of display 0. |
| `no_list` | `no_rows` | The list has no window row of display 0. |
| `line_format` | `list_end` | A list does not end at a line it is known to end at (`MonitoringChannels:`, `Connections:`, `Global monitors ... display N:`, `mMaximumObscuringOpacityForTouch: ...`, `Display: N`); the line it ended at is named. |
| `line_format` | `stray_owner` | A line outside every list holds `ownerUid=`. |
| `line_format` | `orphan_line` | A line in the list comes before its first window row. |
| `line_format` | `legacy_row_in_display_list` | A list per display holds a row of API 24 to 28 (`scale=`). |
| `line_format` | `form_mismatch` | A row differs from the list's first row in form: a quoted or bare name, or which known fields it prints. |
| `line_format` | `transform_mismatch` | Some rows are followed by transform lines and others are not (when the lines after a row are not a transform line with its rows, `unknown_line` or `transform_shape` names them instead). |
| `line_format` | `unknown_line` | A line after a window row that is neither a transform line nor one of its matrix rows. |
| `line_format` | `transform_shape` | A transform line with matrix rows missing, malformed or too many (none for `IDENTITY`, three otherwise). |
| `line_format` | `row_shape` | A window line whose start is of no known form (`N: name=..., [id=N, ]displayId=N`). |
| `line_format` | `row_number` | A row number that is not the next one (rows run 0, 1, 2, ...). |
| `line_format` | `field_twice` | A field that is read appears twice in one line (`ownerUid=`, `touchableRegion=`, `inputConfig=`, `flags=`, `visible=`, `displayId=`, `portalToDisplayId=`, `samsungFlags=`, `pTouchableRegion=`, `1HXY=`). |
| `line_format` | `display_mismatch` | A row's `displayId=` differs from its `Display:` section. |
| `line_format` | `field_value` | A field that is read is named but its value does not read: a value of another form (`visible=yes`), or more text after it before the next `, key=` field. |
| `line_format` | `field_missing` | A field that is read is not there: `ownerUid=`, or both `inputConfig=` and `visible=`, or `flags=` beside `visible=`. |
| `line_format` | `unknown_flag` | A flag name in `inputConfig=` or `flags=` that uiprobe does not know, in a row of display 0 (in another display's list an `inputConfig=` name refuses only when it is not one upper-case word, while `flags=` names (API 24 to 32) refuse there too; a touch on display 0 is assumed to reach only display 0's windows, recalled Android behaviour not checked on a device). |
| `line_format` | `mixed_flags` | `inputConfig=` together with `visible=` or `flags=`. |
| `line_format` | `hex_form` | A `flags=0x...` row that is neither wholly of API 24 to 28 (`scale=`) nor of API 29 and 30 (`globalScale=`, `windowScale=` and `portalToDisplayId=`). |
| `line_format` | `portal_missing` | A row of a form that prints `portalToDisplayId=` (API 29 to 32) without it. |
| `line_format` | `samsung_fields` | A row names `samsungFlags=`, `pTouchableRegion=` or `1HXY=` (the Samsung form, API 36), but the fields after the name do not hold all three at their places with a value the phone printed (for `samsungFlags=0x1`, in another display's list). |
| `line_format` | `samsung_flags` | A window of the display's list prints `samsungFlags=0x1` and is not the app's own window (the same owner uid and title test as for the window at the point; see [Crawling from Python](python.md#crawling-from-python-alpha)), wherever it is in the list and whether or not it takes touches. Two Samsung phones printed that value on the app's own window only; what it means is not known. Without `--package` this command knows no app, so it names this rule for every window with that value, also where the crawl would tap; with `--package PKG` it applies the crawl's test for `PKG`. |
| `region_format` | `region_missing` | A row without `touchableRegion=`. |
| `region_format` | `region_format` | A touchable region that is not one rectangle or `<empty>`. |
| `region_format` | `region_reversed` | A touchable region whose right is left of its left, or bottom above its top. |
| `rotation` | `display_transform_missing` | The display's section prints `logicalSize=` without a display transform line, or the list has `inputConfig` rows and the section lacks either. |
| `rotation` | `display_transform` | The display's section has more than one transform line, or one that is not `transform (ROT_0) (IDENTITY)`. |
| `rotation` | `row_rotated` | A transform line of a row of display 0 rotates (not `ROT_0`). |
| `rotation` | `row_orientation` | A row of display 0 prints `displayOrientation=` other than 0. |
| `rotation` | `viewport_malformed` | A `Viewport` line of display 0 of no known form. |
| `rotation` | `scaled` | A `Viewport` line of display 0 whose logical frame differs from its physical frame. |
| `rotation` | `viewport_missing` | No `Viewport` line of display 0 above the dispatcher. |
| `rotation` | `viewport_disagree` | The `Viewport` lines of display 0 say different orientations (the first that differs is named). |
| `rotation` | `rotated` | The display is rotated (orientation 1 to 3). |
| `rotation` | `number_too_long` | A rotation, orientation or `Viewport` display number the reader reads has more digits than Python converts (4300 by default); with leading zeros it could still be 0, or display 0. |

A rule refuses only what the crawl's check refuses: running the diagnosis changes nothing in how the crawl reads the list.

Device options go after `tap-window` (`uiprobe diagnose tap-window -s SERIAL --json`) or before `diagnose`. When the arguments of `diagnose tap-window` cannot be read (an option in the wrong place, an unknown option such as `--ser SERIAL` before `diagnose`, an extra word, a malformed serial or server address), the usage error (exit 2) shows none of them, only the valid form: `uiprobe [-s SERIAL] diagnose tap-window [-s SERIAL] [--adb PATH] [--adb-server tcp:HOST:PORT] [--no-start-server] [--json] [--show-device-text] [--package PKG]` (this holds for any command line whose words include `diagnose`, unless another command is the one run, as in `uiprobe -s diagnose tap ...`, where `diagnose` is the serial and `tap` reports its own errors; a serial given twice gets `--serial may be given only once`, which names neither). A serial that is well formed but matches no device is not a usage error but a device-selection error (`device_not_found`, exit 4), reported by its code alone like the other errors below. With `--json` the report is a `uiprobe.diagnose_tap_window/1` object (see [Output formats](output.md)). An error (no adb server, no such device, a failed read) is reported by its error code and exit code alone, with a fixed message: the error's own message is not printed, because it can name the device. The exit code is 0 whenever the report was printed, readable or not.

From Python, `Device.diagnose_tap_window(show_device_text=False, owned=None)` returns the report (`to_text()`, `to_dict()`, and `note`, None unless the rule is `samsung_flags` or `app_test` is `"uid_unreadable"`, which the command sets when the uid of `--package` could not be read), and `uiprobe.windowdiag.diagnose_tap_window(text, owned=None)` makes it from a saved `dumpsys input`. `owned` is a test of the app's own windows, as for `Device.window_at`; `--package` passes the crawl's test with the uid `Device.package_uid` read. With `owned` the report's `app_test` is `"tested"` (`APP_TESTED`), and its note says that the window is not the app's own window by the given owned test; the command sets `"crawl_tested"` (`APP_CRAWL_TESTED`) for the test `--package` built, and only then does the note name the crawl's owner uid and title test.

### Testing an app's own windows (`--package`)

`uiprobe diagnose tap-window --package PKG` tests the `samsungFlags=0x1` rows as a crawl of `PKG` does (`uiprobe crawl PKG --press`): such a row of display 0's list is accepted when its window is `PKG`'s own, by the crawl's own owner uid and title test (the same function, `_app_window` in `uiprobe.crawl_driver`, that the crawl calls; see [Crawling from Python](python.md#crawling-from-python-alpha)), and refuses with `samsung_flags` otherwise. Nothing else in the report changes: every other rule, and a `samsungFlags=0x1` row in another display's list (`samsung_fields`), refuse as before.

`PKG` is checked as `uiprobe crawl PACKAGE` checks its package (two or more segments separated by dots, each an ASCII letter followed by letters, digits or underscores; at most 255 characters); an invalid one is a usage error (exit 2) before the device is contacted, and the message does not repeat it.

With `--package` the command sends exactly these reads, in this order, and nothing that presses, types, launches or changes anything:

1. `dumpsys package PKG`: the app's uid and whether other packages share it (`Device.package_uid`, the read the crawl makes at its start; `userId=` or `appId=` and `sharedUser=` of the package's own stanza). This command reads it once; the crawl also reads it again before each tap.
2. `dumpsys input`, as without the option. The window's owner (`ownerUid=`) and title (its name) that the test compares come from this same read; no other read is made for them.

When the uid cannot be read (the package is not installed for user 0, or its stanza does not read), no window counts as the app's, so the verdict is the one without `--package`, and the report always says so in its note, whatever the result (still exit 0, no error code): with rule `samsung_flags` the note adds that a crawl of that app would refuse every tap (`cover_unknown`, `target_uid`, with the tap window check on).

The two reads are separate requests. If another device takes the same serial between them (one is unplugged and another plugged in), the uid can come from one device and the window list from the other; the report cannot tell. A crawl pins its device's transport (`Device.pin_transport`), so its requests never reach a device that later takes the serial; this command does not pin it.

`result: readable` describes the window list only, not a tap: no tap point is checked. A crawl can still refuse a tap on that device, for example with `target_uid` (the app's uid could not be read, or changed, right before the tap) or `covered_by_other` (a window of another app takes touches at the tap's point).

The report does not name `PKG`: it is your own input and is not echoed, in text or JSON, so the notice (no device serial, app or window names, coordinates, accounts or network values) stays true, and window titles are still printed only with `--show-device-text`. The report still tells something about `PKG` on the device to anyone who knows which package you gave: whether a `samsungFlags=0x1` window is its own, and, through the note, whether its uid could be read (for example, whether it is installed). The first line still names `dumpsys input` only, the read the report describes.

## Showing a crawl run as a tree

`uiprobe tree RUN_DIR [--json] [--ids] [--why] [--html OUT.html]` reads the run folder of a pressing crawl and prints what the crawl saw as a tree of screens. It reads only the files of the folder (`run-start.json`, `run.json` when it exists, `transitions.jsonl`, `skipped.jsonl` and `screens/*.json`); it contacts no device and no adb server, and it writes no file (except the one page `--html` asks for, see below). RUN_DIR is used as given; inside it, links and non-regular files are refused. That holds for `screens` (a link or a Windows junction is refused), each screen file and each of the files above: a link, a file that is not a regular file (a FIFO, a folder) and a file with more than one name (a hard link) are never read. A screen file is opened only by the name the folder's listing shows for it. Each file is read for at most its limit plus one byte. A file over 64 MiB (`transitions.jsonl`, `skipped.jsonl`) or 8 MiB (a screen file, `run-start.json`, `run.json`) is skipped. It works on run folders of earlier versions too. It takes no device options: `-s`, `--adb`, `--adb-server` and `--no-start-server` are a usage error, as for `init`.

```console
$ uiprobe tree runs/20261125-101502
com.android.settings (com.android.settings)
├─ Search Settings ·
├─ Network & internet
│  ├─ "Navigate up" [back]
│  ├─ Internet
│  │  ├─ Add network ·
│  │  └─ T-Mobile ·
│  ├─ Tips & support → com.google.android.apps.tips
│  └─ Airplane mode [no change]
└─ Connected devices
   └─ Pair new device → see: Network & internet
· = not pressed, so nothing below it was looked at
[back] = the screen's navigate-up button, which is not pressed
[no change] = pressing it left the same screen
"..." = the element has no visible text; this is its accessibility description
→ PACKAGE = pressing it opened that app
→ see: X = it led to a screen already shown under X
left out of the tree (see --json): rows not sent: 1, texts outside rows: 3, unnamed elements: 1
```

- The screens of the run are the nodes. The edges are the taps that were sent: a tap's start line names the screen and the element, its end line says where it led. BACK and a relaunch are not edges. The root is the screen the first launch led to; if that launch's screen could not be read, has no screen file in the folder, or there is no launch line, the root is the screen of the first recorded tap, and with no tap the tree has no root.
- The text tree is a reading aid; `--json` has everything. Each clickable element of a screen is a row, listed under that screen in document order, pressed or not, except the ones folded into the line of a clickable ancestor (see below). A row is named by the first non-empty text in it (the element, then its descendants, in document order); else by its content description, in double quotes; else it has no name. A visible text anywhere in the row always wins over a description. The other texts of a row are in the `--json` output. In the text output a name longer than 200 characters is cut there and ends with a marker such as `…(+123 chars)`; `--json` keeps the full text. Which of the two names a row has is the `name_from` key of its element in `--json` (`text`, `content_desc`, `password` or `none`); `name` of a description-derived row keeps its double quotes in `--json`, and the text tree writes them itself, with a `\"` for each double quote inside the description. The quotes around a description-derived name are chosen by the constant `uiprobe.cli.tree.DESC_ONLY_ROWS` (`"quoted"`, the default; `"show"` writes the description without quotes and `"json_only"` leaves such rows out of the text tree).
- One line on the screen is one line in the tree: a clickable element that has a clickable ancestor is not a line of its own, it folds into the line of its outermost clickable ancestor, which must be clickable in the saved screen file (a tap logged later on a container does not make it a fold target, and a loop of parents folds nothing) (on Samsung a Wi-Fi row holds a text and a clickable switch named by its description: one line, `Wi-Fi`). The taps on a folded element count as taps on that line: its destination is shown under the line, and when the line already has one, or several folded elements have, the further ones use the `(pressed again)` form, `→ see:` and `→ PACKAGE` as for repeated presses of one row. The ` ·` is on the line only when neither the row nor any folded element was pressed, and `--why` shows the reasons of all of them, also when the row's own tap was not sent. The line is named by a visible text first (the row's own, else the first folded element's), then by a description, else by the row's placeholder (`[icon: CLASS]`, `[row: CLASS]`); a line with folded elements is kept whenever any part of it was pressed. The counts of the last line are per line, not per element: a line is counted once per kind of result. Clickable elements with no clickable ancestor (a row of buttons in a top bar) stay one line each; nothing is folded by position. In `--json` every folded element stays an element as before, with one more key, `folded_into`, the element id of the line it folds into.
- Left out of the text tree, and present in `--json`: texts outside any clickable element; elements with no name (one of which any tap reached a screen stays as a line, so that a pressed path never disappears); the results of a tap other than a new screen (`to` and `also_to` in `--json`: `same_screen`, `other_app`, `not_sent`, `unknown`, `not_read`); and skip records that matched no row (`unmatched`, with `why: "too_many_elements"` for a screen with more than 2000 elements, which is not matched at all). The constant `uiprobe.cli.tree.NOTES_IN_TEXT` names the results that are written after a row's name anyway; it holds `other_app` and `screen_above` (the `→ see:` note).
- The first line names the app and its package, `LABEL (PACKAGE)`. When the run listed other packages (`also_target` in `run-start.json`, see [Listing more apps](safety.md#listing-more-apps-also_target)), it ends with `  also target: PACKAGE1, PACKAGE2` and the screens of those packages are drawn like the app's own, under the row that opened them; `→ PACKAGE` is then written only for a package that was not listed. `--json` has the list as `run.also_target` (only when it is not empty). The recorded list is checked with the rules of the settings (package format and length, distinct, not the app itself, not `com.android.systemui`, not a fixed home app); a list that fails them gives a warning and fails the `transitions.jsonl` check below, like any other bad shape. A screen file of a package that is neither the app nor listed makes that check fail, as before, and a permission package listed in `also_target` never owns screen files (a permission package chosen as the primary target is out of scope: it is the app itself, as before, and this check does not change that): a recorded transition to a permission screen keeps its `→ PACKAGE` display.
- A row that was pressed shows the rows of the screen it led to, indented below it; each screen is listed once, where it is first reached, so loops end. A row that led to a screen already listed gets `→ see: NAME`, where NAME is the name of the row that first opened that screen (the first screen is called `first screen`); when another listed row has the same name, the path from the root is written instead, such as `→ see: Network & internet > Internet`. A tap that led to another app's screen (a screen with no file in the run folder) is written as `→ PACKAGE` after the name, with the package escaped like other device text; nothing is added when the record has no package. When the same row was pressed again and led to another screen that is not listed yet, that screen's rows follow under the heading `(pressed again)`; a further tap that went to another app is the heading `(pressed again) → PACKAGE`. A screen that has no row with a name shows one line `(screen with no named rows)` under the row that led to it (with `--ids` its heading shows instead).
- A row that was not pressed ends with ` ·`: nothing below it was looked at, unless it has a kind label (next item). That includes a tap that was not sent (`to.not_sent` in `--json`) and a tap with no usable end line (the run ended or failed while it was sent, or the end line says `failed`: it may or may not have reached the device, so the row is not counted as pressed; `to.unknown` in `--json`).
- A popup is decided only by the crawl's own dialog rule (`uiprobe.guards.tree._is_dialog`, applied to the saved screen file): the first element without a parent, or the first root, is smaller than the display. A pressed row (or line with folded elements, or `(pressed again)` line) whose destination is a popup ends with ` [popup]`, and the popup's rows are not drawn below it; every further press that reaches the same popup ends with ` [popup]` too, never with `→ see:`. The first screen of the run is drawn as usual even when it is a popup. A screen that only the popup's rows led to is not drawn under anything, so it shows in the section `(screen not reached from a pressed row ...)`. The rows of the popup are in `--json`. The rows of a popup screen are never drawn anywhere in the text tree except under the first screen: a popup that only another popup's rows led to, or that no pressed row led to, is shown as its heading line `(screen not reached from a pressed row SID  ACTIVITY) [popup]` with no rows. Repeated presses that lead to the same popup show one ` [popup]` line, as any identical destinations are merged. The mark is decided from the saved screen file of that screen id: the crawl writes a screen file once per screen id, so a later visit to the same id in a smaller or larger window is not judged. It uses the crawl's own rule and nothing else. The texts outside rows, unnamed elements and unmatched skip records of a popup screen the tree does not draw are counted in the usual items of the last line, while the popup's rows count once, under `rows inside popups` (their results, such as `rows not sent`, are not counted again).
- Kind labels (without `--why`): a row that was not pressed and whose recorded skip reasons include a kind shows the kind in brackets instead of the ` ·`, as `Wi-Fi [toggle]`. The kinds come only from the recorded reasons, through a fixed map in the constant `uiprobe.cli.tree.KIND_LABELS`: `toggle` shows as `[toggle]` (a switch or checkbox), `navigate_up` as `[back]` (the screen's navigate-up button), `slider` as `[slider]` (a slider, or a button whose tap point overlaps one) and `disabled` as `[disabled]`. Two or more kinds share one bracket in that order, as `[toggle, disabled]`. Other reasons (`deny_word`, `confirm_button`, `unlabeled`, `screen_edge` and the rest) are not shown in this view, and a row with no kind among its reasons, or with no recorded reason, keeps the ` ·`. A line with folded elements uses the reasons of all its elements. With `--why` nothing changes: every row keeps ` ·` and all its reasons, as ` · [toggle, disabled]`. The summary line and `--json` are the same either way.
- No-change label: a pressed row, or a folded line (a row with the clickable elements folded into it), whose recorded tap results are all `same_screen` (the main result and every further one in `also_to`) ends with ` [no change]`, as `Airplane mode [no change]`. It is decided only from the recorded results; a pressed row has no dot, so the label is the only mark. A row with a same-screen result and any other result (a screen, another app, a result that could not be read), or with any other attempt on the element (a later tap that was not sent, has no end line, or failed), has no label and is shown as before. The label is used while `NOTES_IN_TEXT` does not hold `same_screen`; with it, the note ` → (same screen)` is written instead. `--why` shows the label as well, and `--json` is unchanged.
- After the tree come the legend lines, one for each mark that appears in this output (`·`, then `[toggle]`, `[back]`, `[slider]`, `[disabled]` in that order, then `[no change] = pressing it left the same screen`, `"..."`, `→ PACKAGE`, `→ see: X`, `[popup]`, `(pressed again)`, `(screen with no named rows)`), and then, when something was left out, one line such as `left out of the tree (see --json): rows that led to another app: 1, rows that stayed on the same screen: 3, rows not sent: 2, rows with an unknown result: 1, rows whose next screen could not be read: 1, rows inside popups: 4, rows named only by a description: 1, texts outside rows: 12, unnamed elements: 4, skip records not matched to a row: 2` (only the non-zero counts; `rows that stayed on the same screen` counts only the same-screen results that no `[no change]` label shows, that is rows with another result or attempt, and rows with no line; `rows inside popups` is the number of lines the text tree would have drawn for the popup screens it opened, each popup counted once, and none for a popup that is drawn anyway; "rows named only by a description" only with `"json_only"`; `rows that led to another app` only when `NOTES_IN_TEXT` does not keep that note). With `--json` there is no legend and no such line.
- `transitions.jsonl` is trusted only as a whole: every line must be exactly as uiprobe writes it (valid JSON with no repeated key in an object, the exact schema names and key sets, a string `action.kind`, booleans as booleans, ids as integers or strings as in the writers, a start line then its end line for each action with ids rising, no blank or extra lines, an `off_target` only on a BACK or relaunch start line that also has `from_screen_id`, and a `restart` only as `true` on a launch start line, and a `replay` only as `true` on a tap start line: a path replay tap, which the tree shows as an ordinary pressed row). The file may end after an end line (a normal, `dump_failed`, `not_sent` or `failed` end), after a start line with no end (Ctrl+C, the time limit, an error, a power loss: only that last tap is unknown), or in the middle of a line (the last line has no newline and is not valid JSON: it is ignored with a warning, and an end line cut short leaves its tap unknown; a last line that is valid JSON of the wrong shape, such as `{}`, fails the file). A screen file of another package than the run's, which uiprobe never writes, fails the file too. If any line fails, every pressed row is unknown (shown with ` ·`), no edges are drawn (every screen except the root is listed as one no row led to), and the one warning is `transitions.jsonl is not in the shape uiprobe writes; no press results are shown`. `→ PACKAGE` is shown only when the start line right after that tap's end line has an `off_target` decided on that very screen (its `from_screen_id` is the end line's screen) and names a non-empty package that differs from the run's package, and the screen has no file in the folder (an `off_target` that names the run's own package makes the tap unknown). A screen that is neither in the run folder nor named by an `off_target` as above is a tap with a screen that could not be read (`to.not_read`); a later visit to the same screen is not evidence.
- Screens that no tap led to (reached by a BACK or a relaunch, or the same screen read again with a different id) follow the tree, each under a line `(screen not reached from a pressed row SID  ACTIVITY)`, and so does anything only they lead to.
- `--ids` adds a line `(screen SID  ACTIVITY)` to each screen. `--why` adds the reasons from `skipped.jsonl` to rows that were not pressed only (a pressed row never shows reasons), such as `· [toggle]`.
- The reasons are matched to rows by recomputing each element's fingerprint from its screen file, as the crawl did. A fingerprint that matches no row is not dropped: `--json` lists it as `unmatched`, and the text tree counts it in the last line. A row that was clickable only at a later visit under the same screen id has no fingerprint match in the screen file, so its skip entry stays unmatched.
- If `run-start.json` has `"record_text": false`, the run folder holds no screen texts: no row has a name, so the text tree shows only the rows that led to a screen (as `[row: CLASS]` or `[icon: CLASS]`) and the first line says so; the rest is counted as unnamed elements. A password field always has the name `[password field]`, also in a run folder whose texts were recorded: the writer blanks its text and names it under `hidden`, and the tree reads that key and the hidden `window` of a `covered_by_other` line as the writer produces them (a `hidden` that does not fit makes the transitions check fail, with the warning it gives for any other malformed line).
- Texts come from the app, so every character that Python's `str.isprintable()` rejects appears as an escape (`\t`, `\n`, `\r`, `\xNN`, `\uXXXX` or `\UXXXXXXXX`), as in the other text output: control and bidirectional characters, Unicode spaces such as the thin space (U+2009) and the no-break space (U+00A0), format characters and so on. Only the ASCII space stays as it is. For example `Nov 25 – Dec 24` with a thin space (U+2009) on each side of the dash is shown as `Nov 25\u2009–\u2009Dec 24`; the dash itself is printable and stays.
- A folder that does not exist or has no usable `run-start.json` is a usage error (exit status 2). A line or file that is damaged, not UTF-8, nested too deeply or refused as above (`run.json`, `transitions.jsonl`, `skipped.jsonl` or a screen file) is a warning on stderr (a `warnings` list in `--json`; they are counted as they happen and the first 20 are kept, followed by `and N more warnings`) and the rest is shown, except that `run-start.json` (it must be a `uiprobe.run_start/1` record with a package) is a usage error. A row keeps at most 50 texts. A line that is not a transition or skip record is a warning too, and so is a `run.json` without `stop_reason` (then `finished` is `false`). The exit status is 0 with warnings. A missing `transitions.jsonl` or `skipped.jsonl` is not a warning, because the crawl makes each at its first line; a missing `run.json` only means the run did not end normally (`finished` is `false` in `--json`).

`--json` prints `uiprobe.crawl_tree/1`: `run`, `root`, `screens` (each with `package`, `activity`, `elements` and `unmatched`, and `popup: true` on a popup screen only), `unreached` and `warnings`. Every element has `element_id`, `kind` (`row` or `text`), `class`, `name`, `name_from`, `texts`, `folded_into` (only on a clickable element inside another clickable one) and, for a row, `pressed`, `action_id`, `to`, `also_to` and `reasons`. A row that merges a version cut off at the bottom of a list with its whole version read after a scroll, and that has more than one tap, also has `action_ids` (the action id of every tap, the one that stands for the row first), and `also_to` has one ending for each further tap, a repeated one included. It holds everything the text tree leaves out.

Where a screen's list is divided, `--json` says which part of the list each element is in. The list of a screen is the element with the most direct children that are, or hold, a clickable element (at least 3). A child of that list that is not clickable, holds no clickable element, and lies between two that do (not before the first and not after the last) is a separator: it is text between rows, not a heading. Its text is the first non-empty text in it (the child itself, then what is below it, in document order); it has none when the run did not record texts (`record_text = false`) or when nothing in it has text. The separators cut the list into groups: group 0 is what comes before the first separator, and a separator starts the next group, so its own text is in that group. Two keys carry this, and both are new keys of `uiprobe.crawl_tree/1` (nothing else changed):

- Every element has `group`: the number of the group it lies in, or `null` when it is outside the list (a toolbar above it, a text below it) or when its screen has no separator.
- A screen that has at least one separator has `groups`: a list with one object for every group, `{"index": 0, "text": null}, {"index": 1, "text": "Privacy"}, ...`, where `text` is the text of the separator that starts the group (`null` for group 0 and for a separator with no text). A screen without a separator has no `groups` key.

For a screen that was scrolled, each read is grouped on its own and then lined up with the groups found so far by the list rows it shares with them: rows a later read shares with an earlier one keep their group, and a separator is placed by where it lies next to those rows (its text is only a check, never the sole reason). A later read whose list is another element (not the same class, resource id and ancestors), or that shares no row with the reads before it, leaves its rows outside any group (`null`); so does every later read when the run did not record texts. Grouping starts from the first read that has a list. Some cases stay uncertain: a separator with no text that no shared row places starts a new group, and rows before the list's first read are placed only when the row is the list child itself. The text tree (without `--json`) is the same as before and shows none of this.

### The tree as an HTML page

`uiprobe tree RUN_DIR --html OUT.html [--ids] [--why]` writes the tree as one self-contained HTML file and prints the path it wrote (one line on stdout; the text tree is not printed). The run folder is read by the same code and with the same checks as for the text tree (the transitions gate, and the refusal of links and of files that are not regular files). `--ids` and `--why` work as they do for the text tree; `--json` together with `--html` is a usage error (exit status 2), as the two outputs are different things and nothing is written.

- It writes exactly one file, and only when nothing is at `OUT.html`: an existing file, a link (also one whose target is missing) and a path whose folder does not exist are a usage error, and nothing is created, changed or deleted. Like `uiprobe init` and `screenshot -o`, it creates the file with a create-only open; every existing folder above the file, as written, is looked at and a symbolic link or a Windows junction among them is a usage error, so the file cannot land through a linked folder. What stays: another process with the same rights can swap a folder above the file for a link between the check and the open (and on Windows a link made at the file's own name in between is followed); this is outside what the command guards against, as for the run folder writer. An `OUT.html` that is inside RUN_DIR (both resolved, so a link to the run folder counts) is a usage error too, so the run folder is never written to.
- There is no script in the file and none is loaded: the page has no `<script>`, no event attribute, no external font, image, style sheet or link, and its head carries `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">`, so a browser that follows it runs nothing and fetches nothing. The only links are in-page links (`#id`). The ids are made from uiprobe's own screen ids, reduced to letters, digits, `_` and `-`; no text from the app is ever used in an id or an attribute. Every string from the run folder (names, texts, descriptions, package and activity names, screen ids, reasons) is escaped as text (`html.escape` with quotes), after control and format characters are shown as escapes exactly as in the text tree, so a name such as `<script>alert(1)</script>` is shown as text. The page holds no more text than the run folder holds. With `"record_text": false` it shows the same placeholders as the text tree, and a password field is `[password field]`.
- Where a screen's list is divided (see `--json` above for the rule), the page leaves a little space between the rows of two groups, and when the separator between them has text, shows that text there in small grey type. It is a plain line of its own (`<li class="between">`), not a row: it is not collapsible, has no ` ·` and cannot be pressed. It adds no row and no script; the text goes through the same escaping as every other text. The text tree has no such space or line.
- The rows are those of the text tree, with the same names, folding, ` ·`, `→ see:`, `→ PACKAGE` and `[popup]`. Each row that has something below it is a collapsible `<details>` with a `<summary>`; the rows of the first level (the rows of the first screen, and of each `(screen not reached from a pressed row ...)` section) that have something below them are open, everything deeper is closed. A row that was not pressed has its name in grey (the CSS class `unpressed`) and keeps the ` ·` or, as in the text tree, its kind label in brackets, so colour is not the only sign; the page has the same legend lines. `→ see: X` is a link to the line of X; a browser that does not open closed folds when it follows a link may need the folds above the target opened by hand.
- Two kinds of closed fold hold what the text tree leaves out. Under a row that ends in `[popup]` there is `Rows inside this popup (N)`: the rows of the popup screen, one per line and not drawn further (a further press of a popup row shows as `(pressed again)`, with `→ PACKAGE` for another app, below it, and other results are lines of the popup's own `Other items` fold; what a popup row led to is in `--json`; a screen that only a popup row led to is in its own section, as in the text tree). Under the row that opened a screen, and under the first line for the first screen, there is `Other items on this screen (N)`: the texts outside rows (`text outside rows: ...`), unnamed elements, rows named only by a description when `DESC_ONLY_ROWS` leaves them out, the results of a press that is not a screen (`result of pressing NAME: → (same screen)`, only for a row whose line does not show `[no change]` (a row with another result or another attempt, or one with no line, such as an unnamed button whose only tap stayed); `(not sent: ...)`, `(result unknown)`, `(could not be read)`) and skip records that matched no row. A popup fold has its own `Other items` fold for the popup screen.
- The legend lines (the same words as the text tree, for each mark that appears in the page, popup rows included, except the popup line, which reads `[popup] = pressing it opened a popup; its rows are in the fold below it`) and the last line are at the bottom of the page. The last line has the items and counts of the text tree's last line, under the start `not shown as rows (in the folds below each screen, and in --json): ` instead of `left out of the tree (see --json): `. Items that look alike are kept as separate lines and counted one by one. The text output keeps its own wording, and does none of the work for the page. There are no screenshots.
- To open the page, open the file in a browser (for example `xdg-open OUT.html`, `open OUT.html`, or double-click it); it needs no server and no network. The page is plain text in UTF-8, so it can also be read in an editor. It holds the texts of the run, which can include personal data, so treat the file as you treat the run folder.

## Creating the settings file

`uiprobe init [PATH]` creates the settings file `uiprobe.toml` (at `./uiprobe.toml` unless you give a path) and prints its path. It needs no device and no adb server, and it never overwrites a file. The file it writes says `press = false`, so `crawl` previews until you set `press = true` (then `crawl PACKAGE --out DIR` presses for real, see [Crawling an app](crawl.md)); it has `runs_dir` only as a comment, so a pressing run asks for `--out DIR` until you set `runs_dir` to an existing folder for the run folders, and its `[limits]` (depth 5, actions 300, minutes 30) are the limits of every pressing crawl that gives no `--depth`, `--actions` or `--minutes`; it says `back_on_other_screens = false`, so a pressing crawl restarts the app on a screen of another app until you set it to `true` (or give `--back-on-other-screens` for one run; `--no-back-on-other-screens` turns it off for one run); it says `scroll_lists = false`, so a pressing crawl turns no mouse wheel over lists until you set it to `true` (or give `--scroll` for one run; `--no-scroll` turns it off for one run), with `scrolls_per_screen = 10`, `scroll_amount = 3` and `scroll_incidents = 2` under `[limits]` for when it is on; the file says `defaults_version = 11`. Details are in [Settings file](settings.md).

## Options

These options work before or after the command, except for `init` and `tree`, which accept only `--json` from this table (see [Settings file](settings.md)), and `diagnose`, which takes them after its check (`uiprobe diagnose tap-window --json`) or before `diagnose`.

| Option | Meaning |
| --- | --- |
| `-s SERIAL`, `--serial SERIAL` | The device to use. May be given once. Without it, the device named in `ANDROID_SERIAL` is used, else the only ready device. `uiprobe devices` lists every device, also when `-s` is given. |
| `--adb PATH` | The adb executable to run (it is run only to start the adb server). |
| `--adb-server tcp:HOST:PORT` | The adb server to talk to. `HOST` is an IP address or `localhost` (which means `127.0.0.1`); IPv6 addresses are written in brackets, `tcp:[::1]:5037`. A scoped IPv6 address such as `tcp:[fe80::1%eth0]:5037` is accepted. Host names are not accepted, because uiprobe never makes a DNS lookup: if you use a host name with adb, write the IP address for uiprobe instead. |
| `--no-start-server` | Never start the adb server; fail if it is not running. |
| `--json` | Print JSON. Errors are printed as JSON on stdout too. |

`uiprobe crawl PACKAGE --press` also writes progress lines to stderr while it presses, for example `progress: 2m 14s of 30m, 37 of 300 actions, 12 screens, depth 3 of 5` (at most one every 5 seconds, the first one after the first action); `--quiet` turns them off, and only a pressing run takes it (given to a preview or a `--dry-run` it is a usage error, as the other pressing options). They never go to stdout, also without `--json`, where stdout holds the result. See [Pressing crawl](crawl.md#pressing-crawl---press).

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
