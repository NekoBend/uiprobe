# Security policy

## Reporting a vulnerability

Report a vulnerability privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. This is the only channel for reports; there is no email address. Please do not open a public issue for a vulnerability.

A dump holds the text on the screen, which may include personal data. Leave such data out of a report, or replace it with made-up text.

## Supported versions

uiprobe is alpha software and has no releases yet. Only the latest commit on the `main` branch is supported.

## What counts as a vulnerability

A vulnerability is a way to make uiprobe break a rule that its documentation states. For example:

- uiprobe connects to an address other than the adb server, or makes a DNS lookup ([network and privacy](docs/network.md)).
- An adb server that uiprobe starts itself gets an adb address variable from the environment, or sends mDNS queries ([network and privacy](docs/network.md)).
- `key` sends a key from its refused list ([safety](docs/safety.md#device-wide-keys-are-refused)).
- The crawl preview presses anything ([safety](docs/safety.md#what-is-sent)).

The limits that the documentation already states are not vulnerabilities. For example, `tap`, `swipe` and `key` press what you name on whatever screen is showing; deny words cannot see icon-only buttons; and a dump contains the text on the screen, also on a screen that forbids screenshots. [docs/safety.md](docs/safety.md) lists these limits. Ideas to narrow them are welcome as ordinary issues.
