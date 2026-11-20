# uiprobe

[![ci](https://github.com/NekoBend/uiprobe/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/NekoBend/uiprobe/actions/workflows/ci.yml) [![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE) [![python](https://img.shields.io/badge/python-3.12%2B-blue)](#requirements)

uiprobe saves the screen structure of an Android app and sends taps, swipes and keys to it. It runs `uiautomator dump` on a device or emulator through `adb`, parses the XML, and gives you the result as a Python object, as JSON, or as an indented text tree. It uses only the Python standard library, and by default it talks only to the local adb server (`--adb-server` or `ADB_SERVER_SOCKET` can name another address; see [Options](docs/commands.md#options)).

## Status

uiprobe is alpha software: the command line, the JSON formats and the Python API may change in any later version. This is a development snapshot, version 0.1.0a1.dev0; no alpha is tagged yet.

Ten commands exist: `devices`, `dump`, `screenshot`, `find`, `tap`, `swipe`, `key`, `launch`, `crawl` and `init`. Of `crawl` only the preview exists: it shows what a crawl would press on the screen showing now and presses nothing, and it is what `crawl` does by default. The pressing crawl is planned, not implemented, and a run that asks for pressing (`--press` or `press = true` in the settings file) is refused. Typing and other ways of operating the app are planned too. The guards have been checked only on Android emulators; real devices and other Android versions may behave differently.

## Safety

- `tap`, `swipe` and `key` send real input to whatever screen is showing, in any app, and press what you name, even a button called "Delete" or "Buy". Only the crawl preview (what `crawl` does by default) uses deny words, and it presses nothing.
- Use an emulator, or a test device with test accounts. Do not run uiprobe on a phone that holds your own accounts, payment methods or messages.
- uiprobe uses the device given with `-s`, else the one named in `ANDROID_SERIAL`, else the only ready device. Run `uiprobe devices` first.
- Deny words see only the text, description, hint and resource id that uiautomator reports. An icon-only button without them (a trash can, a send arrow) has no words to match.
- Automating an app you do not own may break its terms of service. Read them first, and prefer your own builds and test environments.
- A dump holds the text on the screen, which may include personal data. Check saved screens before you share them, including in an issue.

More in [docs/safety.md](docs/safety.md).

## Requirements

- Python 3.12 or newer.
- `adb` from the Android SDK Platform-Tools, and a device or emulator that adb can see.
- No Python dependencies.

CI tests uiprobe on Linux and Windows with Python 3.12, 3.13 and 3.14. The tests that run a fake adb shell script are skipped on Windows, so on Windows no test runs `adb start-server` as a real program. macOS is not tested in CI yet.

## Installation

From the GitHub repository:

```sh
pip install git+https://github.com/NekoBend/uiprobe.git
```

or, to install the `uiprobe` command in its own environment:

```sh
uv tool install git+https://github.com/NekoBend/uiprobe.git
```

## Quick start

```console
$ uiprobe devices
SERIAL            STATE         MODEL
emulator-5554     device        sdk_gphone64_x86_64
0123456789ABCDEF  unauthorized
$ uiprobe -s emulator-5554 dump
com.example.shop/.LoginActivity  (s-23512491bbaf)
e1 FrameLayout
├ e2 TextView "ようこそ"
├ e3 EditText id=email hint="メールアドレス" [tap]
└ e4 Button "ログイン" id=login [tap] [disabled]
  └ e5 ImageView id=icon desc="lock"
$ uiprobe find --text "ログイン"
1 element matches  (s-23512491bbaf)
e4 Button "ログイン" id=login [tap] [disabled] center=540,1860
$ uiprobe -s emulator-5554 tap --id email
tapped e3 EditText id=email hint="メールアドレス" [tap] at 540,660
```

Add `--json` to get JSON instead of text (`uiprobe dump --json`), or `--brief` for a short text view with only the elements that have a label or a `[tap]` or `[scroll]` mark (see [Dumping a screen](docs/commands.md#dumping-a-screen)).

## Commands

| Command | What it does | Details |
| --- | --- | --- |
| `devices` | Lists the devices the adb server knows. | [Listing devices](docs/commands.md#listing-devices) |
| `dump` | Prints the screen showing now as a tree, or as JSON. | [Dumping a screen](docs/commands.md#dumping-a-screen) |
| `screenshot` | Saves a screenshot as a new PNG file. | [Taking a screenshot](docs/commands.md#taking-a-screenshot) |
| `find` | Lists the visible elements that match all of the given `--text`, `--id` and `--desc`. | [Finding elements](docs/commands.md#finding-elements) |
| `tap` | Taps or long-presses an element or a point. | [Tapping](docs/commands.md#tapping) |
| `swipe` | Drags one finger, or scrolls in a direction. | [Swiping and scrolling](docs/commands.md#swiping-and-scrolling) |
| `key` | Presses one key. | [Pressing keys](docs/commands.md#pressing-keys) |
| `launch` | Starts an app and waits until it is in front. | [Launching an app](docs/commands.md#launching-an-app) |
| `crawl` | Previews what a crawl would press, using the settings file; pressing is not available yet. | [Previewing a crawl](docs/crawl.md) |
| `init` | Creates the settings file `uiprobe.toml`; no device needed. | [Settings file](docs/settings.md) |

Options and environment variables: [Options](docs/commands.md#options), [Environment variables](docs/commands.md#environment-variables). JSON shapes and exit codes: [docs/output.md](docs/output.md).

## Python

```python
from uiprobe import Device

device = Device()  # or Device("emulator-5554")
screen = device.dump()
login = screen.find_one(resource_id="login")  # exactly one match, or an error
print(login.text, screen.tap_point(login))
device.tap(login)  # press it; returns the point pressed
```

More in the [Python API guide](docs/python.md).

## Network and privacy

uiprobe connects only to the adb server, which is on `127.0.0.1:5037` unless you choose another address. It makes no other network connection and no DNS lookup. Details, including how uiprobe starts the adb server and the temporary file a dump writes on the device, are in [Network and privacy](docs/network.md).

## Documentation

- [docs/commands.md](docs/commands.md): every command, the options and the environment variables.
- [docs/crawl.md](docs/crawl.md): previewing a crawl, with the reasons, deny words and limits.
- [docs/settings.md](docs/settings.md): the settings file `uiprobe.toml` and `uiprobe init`; `crawl` and `key` read it.
- [docs/safety.md](docs/safety.md): detailed safety notes.
- [docs/output.md](docs/output.md): JSON formats and exit codes.
- [docs/run-folder.md](docs/run-folder.md): the folder and the JSON lines a crawl run writes.
- [docs/python.md](docs/python.md): the Python API.
- [docs/network.md](docs/network.md): network and privacy details.
- [docs/development.md](docs/development.md): running the checks and tests.

## Contributing

Issues are welcome. Report a suspected vulnerability privately instead, as [SECURITY.md](SECURITY.md) describes. While uiprobe is alpha, pull requests are not accepted yet.

## License

MIT. See [LICENSE](LICENSE).
