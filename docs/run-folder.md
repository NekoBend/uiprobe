# The run folder

A crawl run is recorded in one new folder. The library part that writes it is `uiprobe.runlog.RunWriter`. It does not talk to the device, and no command writes a run folder yet.

## Rules

- The folder must not exist, and its parent folder must exist. Nothing is created silently and an existing folder is never reused. If the folder exists or the parent is missing, nothing is written and `RunFolderError` is raised.
- Every file is created new. No file is overwritten, renamed, truncated or deleted, also not after an error.
- Text files are UTF-8 with LF line ends on every system. JSON is escaped like the other uiprobe JSON: inside strings, the control characters U+0000 to U+001F use the standard JSON escapes (`\b`, `\t`, `\n`, `\f`, `\r`, and `\u00XX` for the others), and DEL, the C1 controls (U+007F to U+009F), the line and paragraph separators (U+2028, U+2029) and the bidirectional formatting characters (U+061C, U+200E, U+200F, U+202A to U+202E, U+2066 to U+2069) are written as `\uXXXX`. Other text, such as Japanese, stays readable. A JSON line therefore never holds a line break inside a string.
- JSON never holds `NaN` or an infinite number. Each record is serialized once, by an encoder that refuses them with `ValueError` before any file is opened, and nothing is written.
- File names are fixed names, or a screen id (`s-` and 12 lowercase hex digits, such as `s-0123456789ab`). Text that comes from the app never becomes part of a path.
- Every line or file is flushed and then synced to the disk (`fsync`) before the method returns. A line is therefore complete on disk when the writer returns, also if the program is killed right after.
- If a write fails, the file stays as far as it got, `RunFolderError` names it (`details.path`), and the writer refuses all later writes.
- An interrupt such as Ctrl+C that arrives during a write is not turned into `RunFolderError`: it propagates as `KeyboardInterrupt` itself (or `SystemExit` and the like), without `details.path`. The writer then refuses all later writes, as after a failed write. If closing the file also fails after the interrupt, `RunFolderError` (naming the file) may be raised instead of the interrupt; either way the writer refuses all later writes. Marking the writer on an interrupt is best effort (see [Interrupts](#interrupts)).
- At the end, the line files are closed first, then `summary.txt` is written, and `run.json` last. A folder whose `run.json` is missing or is not complete JSON did not end normally. This holds one way only: a `run.json` that looks complete does not prove a normal end, because `finish()` can still fail at the final sync after the bytes are in the file. Only a `finish()` that returned without an error proves it.
- A writer is made only with `RunWriter.create`, which makes the folder. Calling `RunWriter(...)` directly raises `TypeError`, so a writer can never write into a folder it did not make.

## Files

| File | Written | Content |
| --- | --- | --- |
| `run-start.json` | when the folder is made | The settings the run started with. |
| `transitions.jsonl` | one line per event | Each action that is about to be sent, and where it led. Made at the first line. |
| `skipped.jsonl` | one line per skipped element | Elements that were not pressed. Made at the first line. |
| `screens/<screen_id>.json` | the first time a screen id is seen | The screen, in the form of `uiprobe dump --json` (`uiprobe.screen/1`). |
| `screens/<screen_id>.png` | the first time a screenshot is given for a screen id | A PNG image. Anything that does not start with the PNG signature is refused and no file is made. |
| `summary.txt` | at the end | Per screen, the labels of its buttons. |
| `run.json` | at the very end | How the run ended. It is written last, after the line files are closed and `summary.txt` is written. |

The `screens` folder is made when the first screen file is written.

## JSON formats

`run-start.json` and `run.json` hold one JSON object (indented). The `.jsonl` files hold one compact JSON object per line. Times are ISO 8601 with a UTC offset (the local one unless the caller gives a time with its own), to the second, such as `2026-03-01T09:30:15+09:00`.

- `uiprobe.run_start/1`: `started_at`, `package`, `limits` (`depth`, `actions`, `minutes`), `deny` (the deny words in use, in the given order), `device` (`serial`).
- `uiprobe.transition_start/1`: `recorded_at`, `action_id` (counts from 1), `action` and, when known, `from_screen_id`. `action` has `kind` (for example `launch`, `tap`, `back`, `relaunch`) and, when given, `point` (`[x, y]`) and `element`. The line is written before the action is sent.
- `uiprobe.transition_end/1`: `recorded_at`, `action_id`, `dump_failed` (`true` when the screen after the action could not be read) and, only when `dump_failed` is `false`, `to_screen_id`.
- `uiprobe.skip/1`: `recorded_at`, `screen_id`, `fingerprint`, `reasons` (the reasons in the order the caller gives them; the writer does not sort them) and `element`. One line is written for each different combination of `screen_id`, `fingerprint` and `reasons`; repeats are not written.
- `uiprobe.run/1`: `ended_at`, `stop_reason`, `screens` (how many screens were seen) and `actions` (how many actions were sent).

`element` (in `uiprobe.transition_start/1` and `uiprobe.skip/1`) is written in the form the caller gives: an `Element` is written in its `uiprobe.screen/1` form, and a mapping is written as given. The writer does not check its fields.

## summary.txt

For each screen that was written, one heading line (package, activity and the screen id, as in the indented tree output of `uiprobe dump`) followed by one line for each clickable element that has a label (its text, or else its content description). Labels come from the app, so they are quoted and escaped the same way as in the indented tree output: control and bidirectional characters appear as `\uXXXX`.

## Interrupts

`RunWriter.critical()` is a context manager that marks the time while a line or file is written; the read-only property `writing` is `True` inside it. Each write method keeps the write and the writer's record of it (for example that a screen or a skip was already written) inside one such block, so `writing` stays `True` until the write is recorded. A program that handles Ctrl+C can look at `writing` and postpone the interrupt until the write is complete. The library installs no signal handler.

When an interrupt does reach a write, the writer marks itself as failed and refuses later writes, but only as far as it can: an asynchronous exception such as `KeyboardInterrupt` can arrive between any two steps, also after the point where the mark is made, so the mark is best effort and cannot be complete. The reliable way is the one above: a program that handles Ctrl+C postpones it while `writing` is `True`.

## Limits

Files and folders are only ever created new: an existing file is never opened. But if another program running with the same rights replaces the run folder or the `screens` folder with a link (or, on Windows, a junction) during a run, later new files can be created where that link points. uiprobe does not defend against such a program, which could just as well write there itself. That reasoning holds only when the other program has the same rights: if you run uiprobe with higher rights (as an administrator or root), choose a run folder whose parent folders no other user can write to, not a shared folder such as a common temporary folder, because a program with lower rights could otherwise move where uiprobe creates its new files.

The same holds for a program that appends to `transitions.jsonl` or `skipped.jsonl` during a run: the writer keeps its own position in the file, so its next line can overwrite what was appended. The writer assumes it is the only one writing into its run folder.

The offline file guard (`tests/test_offline_guard.py`) checks that each of the four file-creating calls appears once, in `runlog.py`. It does not stop another uiprobe module from importing runlog's private helpers that make those calls. The two open helpers (`_create_text`, `_create_binary`) and `_make_dir` can only create new files and folders (an existing file or folder is an error). `_put` writes to whatever handle it is given and does not check where the handle came from. Review covers who calls these helpers.
