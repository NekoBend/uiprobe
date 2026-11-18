# The run folder

A crawl run is recorded in one new folder. The library part that writes it is `uiprobe.runlog.RunWriter`. It does not talk to the device, and no command writes a run folder yet.

## Rules

- The folder must not exist, and its parent folder must exist. Nothing is created silently and an existing folder is never reused. If the folder exists or the parent is missing, nothing is written and `RunFolderError` is raised.
- Every file is created new. No file is overwritten, renamed, truncated or deleted, also not after an error.
- Text files are UTF-8 with LF line ends on every system. JSON escapes control characters and bidirectional formatting characters as `\uXXXX`, like the other uiprobe JSON.
- File names are fixed names, or a screen id (`s-` and 12 lowercase hex digits, such as `s-0123456789ab`). Text that comes from the app never becomes part of a path.
- Every line or file is flushed and then synced to the disk (`fsync`) before the method returns. A line is therefore complete on disk when the writer returns, also if the program is killed right after.
- If a write fails, the file stays as far as it got, the error names it (`details.path`), and the writer refuses all later writes. A folder without `run.json` did not end normally.

## Files

| File | Written | Content |
| --- | --- | --- |
| `run-start.json` | when the folder is made | The settings the run started with. |
| `transitions.jsonl` | one line per event | Each action that is about to be sent, and where it led. Made at the first line. |
| `skipped.jsonl` | one line per skipped element | Elements that were not pressed. Made at the first line. |
| `screens/<screen_id>.json` | the first time a screen id is seen | The screen, in the form of `uiprobe dump --json` (`uiprobe.screen/1`). |
| `screens/<screen_id>.png` | the first time a screenshot is given for a screen id | A PNG image. Anything that does not start with the PNG signature is refused and no file is made. |
| `summary.txt` | at the end | Per screen, the labels of its buttons. |
| `run.json` | at the very end | How the run ended. It is written last, after `summary.txt`. |

The `screens` folder is made when the first screen file is written.

## JSON formats

`run-start.json` and `run.json` hold one JSON object (indented). The `.jsonl` files hold one compact JSON object per line. Times are ISO 8601 with a UTC offset (the local one unless the caller gives a time with its own), to the second, such as `2026-03-01T09:30:15+09:00`.

- `uiprobe.run_start/1`: `started_at`, `package`, `limits` (`depth`, `actions`, `minutes`), `deny` (the deny words in use, in the given order), `device` (`serial`).
- `uiprobe.transition_start/1`: `recorded_at`, `action_id` (counts from 1), `action` and, when known, `from_screen_id`. `action` has `kind` (for example `launch`, `tap`, `back`, `relaunch`) and, when given, `point` (`[x, y]`) and `element` (the form of an element in `uiprobe.screen/1`). The line is written before the action is sent.
- `uiprobe.transition_end/1`: `recorded_at`, `action_id`, `dump_failed` (`true` when the screen after the action could not be read) and, only when `dump_failed` is `false`, `to_screen_id`.
- `uiprobe.skip/1`: `recorded_at`, `screen_id`, `fingerprint`, `reasons` (the reasons in the order the caller gives them; the writer does not sort them) and `element`. One line is written for each different combination of `screen_id`, `fingerprint` and `reasons`; repeats are not written.
- `uiprobe.run/1`: `ended_at`, `stop_reason`, `screens` (how many screens were seen) and `actions` (how many actions were sent).

## summary.txt

For each screen that was written, one heading line (package, activity and the screen id, as in the indented tree output of `uiprobe dump`) followed by one line for each clickable element that has a label (its text, or else its content description). Labels come from the app, so they are quoted and escaped the same way as in the indented tree output: control and bidirectional characters appear as `\uXXXX`.

## Interrupts

`RunWriter.critical()` is a context manager that marks the time while a line or file is written; the read-only property `writing` is `True` inside it. A program that handles Ctrl+C can look at `writing` and postpone the interrupt until the write is complete. The library installs no signal handler.
