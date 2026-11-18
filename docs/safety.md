# Safety

uiprobe is alpha software, and `tap`, `swipe` and `key` send real input to a device. This page gathers the safety notes. Most notes below restate the other pages in short form; the links lead to the full rules.

## What is sent

- `tap`, `swipe` and `key` send real input to whatever screen is showing, in any app, and they press what you name, also a button called "Delete" or "Buy".
- They act on the screen showing now. The foreground app is not checked, and there is no deny-word guard. A swipe that starts at a screen edge can trigger a system gesture.
- With `tap --xy` and with `swipe --from ... --to ...` there is no dump and no check: the point is pressed, or the swipe sent, as given, because you named the points.
- Each press is sent once and never resent, not even after a lost connection, because a resend could press twice. Exit 14 (`input_failed`) does not prove that nothing was pressed: `input` may have acted before it failed. Look at the screen before you repeat the command.
- Only `crawl --dry-run` uses deny words, and it presses nothing.

Details: [Commands](commands.md).

## Real devices and test accounts

- Use an emulator, or a test device with test accounts. Do not run uiprobe on a phone that holds your own accounts, payment methods or messages.
- uiprobe uses the device given with `-s`, else the one named in `ANDROID_SERIAL`, else the only ready device. Run `uiprobe devices` first.
- The guards have been checked only on Android emulators; real devices and other Android versions may behave differently.

## What deny words cannot see

- Deny words see only the text, description, hint and resource id that uiautomator reports. An icon-only button without them (a trash can, a send arrow) has no words to match.
- `--tap-unlabeled` allows pressing elements that have no text, description or hint. This also allows icon-only buttons that deny words cannot see, such as a trash can.
- Substring matching also over-blocks (`Posture` contains `post`); a miss is worse than an over-block. The known limits are listed in [Previewing a crawl](crawl.md).

## Device-wide keys are refused

`key` refuses a fixed list of key names and name prefixes with a usage error (exit 2) before the adb server is contacted, whether the key is given as an alias or as a name. The list covers keys that switch the device off, place calls, change the sound, media playback or brightness, take screenshots, start assistants or open other apps. Every other key is sent, including `home` and `recents`, which can leave the current app. Like `tap` and `swipe`, `key` sends to whatever screen is showing, in any app. The groups and the keys that stay allowed are listed in [Pressing keys](commands.md#pressing-keys).

## What the guards cannot see

- The preview judges one screen as it is. It cannot see what a press would lead to, and it does not look at the pixels, only at what `uiautomator` reports.
- The screen-edge areas use the sizes measured on an API 36 emulator for every device for now, in pixels, not scaled to the device's density.
- The foreground app is not checked by `tap`, `swipe` or `key`.

## Terms of service

Automating an app you do not own may break its terms of service. Read them first, and prefer your own builds and test environments.

## Personal data in dumps

- A dump holds the text on the screen, which may include personal data such as names, addresses and messages. Check saved screens before you share them, also in an issue.
- Android lets an app forbid screenshots of a screen (`FLAG_SECURE`), but `uiautomator` still returns that screen's structure and text. So a dump of such a screen, and any copy of it you save, contains that text, which may include personal data. For example, on Android emulators at API 24 and API 36, Chrome's Incognito tab had `FLAG_SECURE`: Android's screenshot command, `screencap`, gave empty output on API 24 and an almost entirely black image on API 36, while `uiautomator dump` and `uiprobe dump` returned the full tree with its text.
