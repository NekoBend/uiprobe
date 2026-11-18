# Security policy

## Reporting a vulnerability

Report a suspected vulnerability privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. This is the only channel for reports; there is no email address. Please do not open a public issue for a vulnerability. When you are not sure whether something is a security problem, report it privately anyway.

A dump can hold any text that Android reports to `uiautomator`, which may include personal data. Leave such data out of a report, or replace it with made-up text.

## Supported versions

uiprobe is alpha software, and no version is tagged yet. Only the latest commit on the `main` branch is supported.

## Examples

Any way to make uiprobe break a rule that its documentation states is a vulnerability. For example:

- uiprobe connects to an address other than the adb server, or makes a DNS lookup ([network and privacy](docs/network.md)).
- An adb server that uiprobe starts itself gets `ADB_SERVER_SOCKET`, `ANDROID_ADB_SERVER_ADDRESS` or `ANDROID_ADB_SERVER_PORT` from the environment, or sends mDNS queries ([network and privacy](docs/network.md)). For mDNS, please include your Platform-Tools version (`adb version`); the docs record a check with Platform-Tools 37.0.1.
- `key` sends a key from its refused list ([safety](docs/safety.md#device-wide-keys-are-refused)).
- The crawl preview presses anything ([safety](docs/safety.md#what-is-sent)).

[docs/safety.md](docs/safety.md) also lists limits that are known and intended for now. For example, `tap`, `swipe` and `key` send real input to whatever screen is showing, in any app, without checking the foreground app; and deny words see only the text, description, hint and resource id that `uiautomator` reports, so an icon-only button without them has no words to match. A limit as the docs describe it can go in an ordinary issue. If you find a security impact that the docs do not describe, report it privately.
