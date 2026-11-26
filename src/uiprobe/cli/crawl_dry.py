"""The crawl decision, the settings file, the run folder checks and the dry run."""

from __future__ import annotations

import argparse  # noqa: TC003 (a runtime import in the former cli.py)
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from uiprobe.cli.output import _emit, _printable
from uiprobe.cli.parser import _GuidedUsageError, _press_only, _UsageError
from uiprobe.crawl_preview import check_package
from uiprobe.device import Device  # noqa: TC001 (a runtime import in the former cli.py)
from uiprobe.errors import UiprobeError
from uiprobe.guards import REASONS, Policy
from uiprobe.pngfile import check_new_path, depends_on_a_drive_folder
from uiprobe.runlog import RunFolderError, _check_folder, _new_folder
from uiprobe.settings import (
    SETTINGS_FILE,
    Settings,
    SettingsError,
    _folder_form_base,
    _shown,
    _without_trailing_separators,
    load_settings,
    policy_from_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from uiprobe.crawl_preview import Preview
    from uiprobe.guards import Decision, Detail
    from uiprobe.model import Screen


# Why a run presses (the "press_source" values): --press, or press = true.
_PressSource = Literal["option", "file"]


# Where the run folder came from (the "out_source" values): --out, or runs_dir.
_OutSource = Literal["option", "file"]


# The most names tried in runs_dir for one run: NAME, then NAME-2 to NAME-99.
_MAX_RUN_NAME = 99


CRAWL_PREVIEW_SCHEMA = "uiprobe.crawl_preview/1"


_PLAIN_PATH_RE = re.compile(r"[A-Za-z0-9_./][A-Za-z0-9_./-]*")


# On Windows a backslash and a colon need no quoting in cmd or PowerShell, so a
# drive path such as C:\Users\me\uiprobe.toml is named as it is.
_PLAIN_WINDOWS_PATH_RE = re.compile(r"[A-Za-z0-9_./\\:][A-Za-z0-9_./\\:-]*")


_ON_WINDOWS = sys.platform == "win32"


def _load_settings(path: str, *, warn: bool = True) -> Settings:
    """Read the settings file `path`; warnings go to stderr, errors are usage errors.

    With `warn` False the caller writes the warnings (`_settings_warnings`).
    """
    try:
        settings = load_settings(path)
    except SettingsError as exc:
        raise _UsageError(_printable(str(exc))) from None
    if warn:
        _settings_warnings(settings)
    return settings


def _settings_warnings(settings: Settings) -> None:
    """Write the warnings about the settings file to stderr."""
    for warning in settings.warnings:
        _emit(f"uiprobe: warning: {_printable(warning)}", stream=sys.stderr)


def _is_missing(path: str) -> bool:
    """Tell whether nothing is at `path`; a broken link or entry counts as there.

    The path is looked up as given, not normalised by `Path` (which drops a
    trailing ``/.``), so ``rules.toml/.`` after a file is missing, as the
    settings reader, which opens it as given, would find it.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except (OSError, ValueError):
        return False  # let the reader name the problem
    return False


def _check_config_path(path: str | None) -> None:
    """Refuse a ``--config`` value that ends with a path separator or ``/.``.

    A folder-like name such as ``rules.toml/`` or ``rules.toml/.`` would
    otherwise lose its ending when turned into a path and name the file
    ``rules.toml``. The message names the file itself, never an init command
    for the folder form.
    """
    if path is None:
        return
    file = _folder_form_base(path)
    if file is None:
        return
    msg = (
        f"--config {_shown(path)}: a settings file path cannot end with a path"
        ' separator or "/." (that is the folder form of a path)'
    )
    if file:
        hint = _hint_path(file)
        if hint == "PATH":
            hint = "PATH, the same path without that ending"
        msg += f"; to use the file, give --config {hint}"
    raise _UsageError(msg)


def _hint_path(path: str) -> str:
    """Return `path` for a printed hint; one that needs shell quoting gives PATH."""
    pattern = _PLAIN_WINDOWS_PATH_RE if _ON_WINDOWS else _PLAIN_PATH_RE
    if pattern.fullmatch(str(path)) is None:
        return "PATH"
    return path


def _create_line(path: str) -> str:
    """Return the command that creates ``path``; a path needing quoting gives PATH."""
    return f"uiprobe init {_hint_path(path)}"


def _crawl_settings(args: argparse.Namespace) -> Settings:
    """Return the settings for ``crawl``; no file is a usage error with guidance.

    An explicit ``--config`` path that is genuinely absent gets the same
    guidance, naming that path; an unreadable file or a broken link is left to
    the reader, which names the problem.
    """
    _check_config_path(args.config)
    path = SETTINGS_FILE if args.config is None else args.config
    if _is_missing(path):
        looked = f"./{SETTINGS_FILE}" if args.config is None else path
        create = "uiprobe init"
        if args.config is not None:
            # Name the looked-for path only when it needs no shell quoting.
            create = _create_line(path)
        # Keep a --dry-run or --press the caller gave: a file that says
        # press = true presses without --dry-run, and --press stays visible.
        switch = " --dry-run" if args.dry_run else ""
        name = f"uiprobe crawl {args.package}{switch} --config PATH"
        if args.press or any(given for _option, given in _press_only(args)):
            # A pressing run has more options (--out, limits, device) than a
            # line could repeat safely: name the one thing to add.
            name = "add --config PATH to the same command"
        raise _GuidedUsageError(
            (
                "crawl needs a settings file and none was found "
                f"(looked for {_shown(looked)})",
                f"create it with:    {create}",
                f"or name one with:  {name}",
            ),
            {"looked_for": looked, "create": create, "name_one": name},
        )
    # The caller writes the warnings once it knows whether the run presses.
    return _load_settings(path, warn=False)


@dataclass(frozen=True)
class _CrawlSetup:
    """What a crawl runs with, decided before any device is contacted.

    ``press_source`` is None for the preview, else why the run presses;
    ``out`` is the run folder of a pressing run and ``out_source`` where it
    came from (both None for the preview).
    """

    policy: Policy
    settings: Settings
    press_source: _PressSource | None
    out: str | None
    out_source: _OutSource | None


def _refuse_press_only(args: argparse.Namespace, why: str) -> None:
    """Refuse the first option that only a pressing run takes, if one was given."""
    given = [option for option, value in _press_only(args) if value]
    if given:
        msg = f"{given[0]} is used only when the crawl presses{why}"
        raise _UsageError(msg)


# The run folder rule below is also named in the switch lines of
# `_preview_guidance` ("add --press" with runs_dir, else "--press --out DIR",
# and the "to always press" line): update them, their tests and
# docs/crawl.md when this rule changes.
def _file_press_out(
    args: argparse.Namespace, settings: Settings, press_source: _PressSource
) -> tuple[str, _OutSource]:
    """Return the run folder of a pressing run and where it came from.

    The rule is the same for --press and press = true: --out when given
    (relative to the working folder); else a new folder inside the settings
    file's runs_dir, named ``PACKAGE-YYYYMMDD-HHMMSS`` in local time, with
    ``-2`` to ``-99`` added when that name is taken; else a usage error that
    asks for --out. The parent must exist and be a folder, not a link; only
    the run folder is made in it, later, by `crawl_run` with a create-only
    mkdir (a folder that appears there before that ends the run unshared).

    Raises:
        _GuidedUsageError: No --out and no runs_dir.
        RunFolderError: The runs_dir folder is missing, not a folder or a
            link, or every name up to ``-99`` is taken.
    """
    if args.out is not None:
        return args.out, "option"
    shown_file = _shown(str(settings.source))
    if settings.runs_dir is None:
        first = (
            "a pressing crawl needs a folder to record the run into, and none was given"
        )
        if press_source != "option":
            first += f" (the settings file {shown_file} says press = true)"
        lines = [
            first,
            "give one now:      add --out DIR to the same command"
            " (DIR must not exist yet)",
            f'or set it once:    runs_dir = "uiprobe-runs" in {shown_file}'
            " (a real folder that exists, counted from the settings file's folder;"
            " each run gets a new folder inside it)",
        ]
        if press_source != "option":
            lines.append(
                "or preview:        add --dry-run and remove press-only options"
                " such as --depth (a preview presses nothing)"
            )
        # The text goes out line by line; JSON keeps one message string.
        raise _GuidedUsageError(lines, {})
    parent = _runs_dir_parent(args, settings.runs_dir, shown_file)
    base = f"{args.package}-{_run_stamp()}"  # a plain str: see _crawl_policy
    for number in range(1, _MAX_RUN_NAME + 1):
        name = base if number == 1 else f"{base}-{number}"
        candidate = os.path.join(parent, name)  # noqa: PTH118 - kept as given
        try:
            check_new_path(candidate)
        except FileExistsError:
            continue
        except (OSError, ValueError) as error:
            msg = f"cannot check the run folder {_shown(candidate)}: {error}"
            raise RunFolderError(msg, details={"path": candidate}) from None
        return candidate, "file"
    msg = (
        f"cannot make a run folder in {_shown(parent)} (runs_dir in"
        f" {shown_file}): {base} and {base}-2 to {base}-{_MAX_RUN_NAME} all"
        " exist; remove some, or add --out DIR"
    )
    raise RunFolderError(msg, details={"path": parent})


def _run_folder_parent(
    folder: str,
    *,
    windows: bool | None = None,
    splitdrive: Callable[[str], tuple[str, str]] = os.path.splitdrive,
    dirname: Callable[[str], str] = os.path.dirname,
) -> str:
    """Return the folder that `folder` goes in, ignoring trailing separators.

    ``runs/new/`` goes in ``runs``; a root stays a root. `windows`,
    `splitdrive` and `dirname` default to this platform's rules (tests pass
    the other platform's).
    """
    stripped = _without_trailing_separators(
        folder, windows=windows, splitdrive=splitdrive
    )
    return dirname(stripped)


def _check_run_folder_leaf(
    folder: str,
    *,
    windows: bool | None = None,
    splitdrive: Callable[[str], tuple[str, str]] = os.path.splitdrive,
) -> None:
    """Refuse `folder` when something is at it once trailing separators go.

    ``taken/`` names the file or dangling link ``taken`` as a folder; the
    lookup of ``taken/`` itself fails, so the name without the separators is
    checked as `check_new_path` checks a path. Nothing is created.
    """
    stripped = _without_trailing_separators(
        folder, windows=windows, splitdrive=splitdrive
    )
    if stripped != folder:
        _check_folder(stripped)


def _check_run_folder_parent(folder: str) -> None:
    """Refuse the run folder `folder` when its parent is missing or no folder.

    `folder` is absolute (as `_new_folder` returns it). Links above it are
    followed, as when the run makes the folder. Nothing is created.
    """
    parent = _run_folder_parent(folder)
    try:
        info = os.stat(parent)  # noqa: PTH116 - the path as given
    except (FileNotFoundError, NotADirectoryError):
        msg = (
            f"cannot make the run folder {_shown(folder)}: the folder it goes in,"
            f" {_shown(parent)}, does not exist; create it, or name another one"
        )
        raise RunFolderError(msg, details={"path": folder}) from None
    except (OSError, ValueError) as error:
        msg = f"cannot check the folder {_shown(parent)}: {error}"
        raise RunFolderError(msg, details={"path": folder}) from None
    if not stat.S_ISDIR(info.st_mode):
        msg = (
            f"cannot make the run folder {_shown(folder)}: {_shown(parent)} is not"
            " a folder"
        )
        raise RunFolderError(msg, details={"path": folder})


def _run_stamp() -> str:
    """The local time for a run folder name: YYYYMMDD-HHMMSS, no zone."""
    return f"{datetime.now().astimezone():%Y%m%d-%H%M%S}"


def _runs_dir_parent(args: argparse.Namespace, runs_dir: str, shown_file: str) -> str:
    """Return the runs_dir folder, checked: it exists, is a folder, is no link.

    A relative runs_dir counts from the folder of the settings file, taken
    from the path as given (--config, or ./uiprobe.toml); ``~`` is not
    expanded. Links in earlier parts of the path are followed, as for --out.
    """
    if depends_on_a_drive_folder(runs_dir):
        msg = (
            f"runs_dir {_shown(runs_dir)} in {shown_file} depends on a drive's"
            " own working folder; give a full path with its drive, or a path"
            " relative to the settings file"
        )
        raise RunFolderError(msg, details={"path": runs_dir})
    config = SETTINGS_FILE if args.config is None else args.config
    parent = os.path.join(os.path.dirname(config), runs_dir)  # noqa: PTH118, PTH120
    where = f"{_shown(parent)} (runs_dir in {shown_file})"
    try:
        info = os.lstat(parent)
    except (FileNotFoundError, NotADirectoryError):
        msg = (
            f"the folder for run folders {where} does not exist; create that"
            " folder, or add --out DIR"
        )
        raise RunFolderError(msg, details={"path": parent}) from None
    except (OSError, ValueError) as error:
        msg = f"cannot check the folder for run folders {where}: {error}"
        raise RunFolderError(msg, details={"path": parent}) from None
    # A link or a Windows junction is refused here. Only these are checked:
    # os.lstat reports just the name-surrogate reparse points (symbolic links
    # and junctions) as links, and other reparse points, such as cloud-file
    # placeholders, are resolved by the system, so refusing on the reparse
    # point attribute alone would refuse ordinary synced folders.
    # The lstat result decides first (a junction is a mount point reparse
    # point), so a swap after it cannot pass; isjunction is a second check.
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", object())
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_reparse_tag", 0) == mount_point
        or os.path.isjunction(parent)
    ):
        msg = (
            f"the folder for run folders {where} is a link or junction; give"
            " the folder it points to in runs_dir, or add --out DIR"
        )
        raise RunFolderError(msg, details={"path": parent})
    if not stat.S_ISDIR(info.st_mode):
        msg = (
            f"the folder for run folders {where} is not a folder; fix runs_dir,"
            " or add --out DIR"
        )
        raise RunFolderError(msg, details={"path": parent})
    return parent


def _crawl_policy(args: argparse.Namespace) -> _CrawlSetup:
    """Check the crawl options, read the settings, decide whether it presses.

    A run presses when --press is given, or when the settings file says
    press = true and --dry-run is not given. What the options alone decide is
    checked before the file is read. No device is contacted.
    """
    if args.press and args.dry_run:
        msg = "--press and --dry-run cannot be combined"
        raise _UsageError(msg)
    # A plain str, whatever the subclass (as crawl_run does), for the check,
    # the run folder's name and the run itself.
    args.package = str.__str__(args.package)
    try:
        check_package(args.package)
    except ValueError as exc:
        raise _UsageError(_printable(str(exc))) from None
    for word in args.deny:
        try:
            Policy().with_deny_words([word])
        except ValueError:
            msg = f"--deny word is empty after normalization: {word!r}"
            raise _UsageError(msg) from None
    if args.dry_run:
        _refuse_press_only(args, ", and --dry-run never presses")
    settings = _crawl_settings(args)
    press_source: _PressSource | None = None
    if args.press:
        press_source = "option"
    elif settings.press and not args.dry_run:
        press_source = "file"
    if press_source is not None:
        # Before the settings warnings: a pressing run whose plan cannot be
        # shown is refused before anything is written, as with --press, where
        # this was checked before the file was read.
        _check_plan_stream(args)
    _settings_warnings(settings)
    if press_source is None and not args.dry_run:
        _refuse_press_only(
            args,
            ": add --press, or set press = true in the settings file"
            f" {_shown(str(settings.source))}",
        )
    out: str | None = None
    out_source: _OutSource | None = None
    if press_source is not None:
        # --out, else runs_dir, else a usage error: only _file_press_out decides.
        out, out_source = _file_press_out(args, settings, press_source)
        # Refusal only, before the device is contacted: the same checks the
        # run makes before it creates the folder, and that its parent is an
        # existing folder (nothing is created here; the run's own checks and
        # its create-only mkdir still follow).
        folder = _new_folder(out)
        _check_run_folder_leaf(folder)
        _check_run_folder_parent(folder)
    policy = policy_from_settings(
        settings,
        extra_deny=args.deny,
        tap_unlabeled=True if args.tap_unlabeled else None,
    )
    return _CrawlSetup(policy, settings, press_source, out, out_source)


def _detail_text(decision: Decision, detail: Detail) -> str:
    """Describe what one deny word matched, for example ``"pay" in text of e3``."""
    text = f'"{_printable(detail.word)}"'
    if detail.field:
        text += f" in {_printable(detail.field)}"
    if detail.element_id and detail.element_id != decision.element.id:
        text += f" of {_printable(detail.element_id)}"
    return text


def _reasons_text(decision: Decision) -> str:
    parts: list[str] = []
    for reason in decision.reasons:
        part: str = reason
        if reason in ("deny_word", "irreversible_screen", "confirm_button"):
            found = [
                _detail_text(decision, d)
                for d in decision.details
                if d.reason == reason and d.word
            ]
            if found:
                part += f" ({'; '.join(found)})"
        parts.append(part)
    return ", ".join(parts)


def _decision_center(screen: Screen, decision: Decision) -> tuple[int, int] | None:
    """Return the ``center`` to show for a decision, without scanning the screen.

    It is the point the judgement chose, left out when the element is not
    visible or is covered (as ``Screen.tap_point`` would refuse it). A screen
    that was too large to judge has no point; then the center of the visible
    part is shown, which costs one element only. That part comes from
    ``Screen.visible_bounds``, which always uses 8 pixels, even when the
    policy's ``min_visible_px`` is larger (the element is skipped anyway).
    """
    if "screen_too_large" in decision.reasons:
        visible = screen.visible_bounds(decision.element)
        return None if visible is None else visible.center
    if "not_visible" in decision.reasons or "covered" in decision.reasons:
        return None
    return decision.point


def _preview_text(
    result: Preview,
    settings_path: str,
    *,
    dry_run: bool,
    guidance: Sequence[str] = (),
) -> str:
    screen = result.screen
    shown = {True: "shown", False: "hidden", None: "unknown"}[result.keyboard_shown]
    foreground = "unknown" if result.foreground is None else result.foreground
    pressable = result.judgement.pressable
    skipped = result.judgement.skipped
    lines = [
        f"preview for {_printable(result.package)} on screen {screen.screen_id};"
        " nothing was pressed",
        f"settings: {_printable(_shown(settings_path))}",
        f"foreground: {_printable(foreground)}, keyboard: {shown}",
        f"would press ({len(pressable)}):",
    ]
    lines.extend(
        "  " + screen._describe_line_at(d.element, _decision_center(screen, d))  # noqa: SLF001
        for d in pressable
    )
    lines.append(f"would not press ({len(skipped)}):")
    lines.extend(
        "  "
        + screen._describe_line_at(d.element, _decision_center(screen, d))  # noqa: SLF001
        + ": "
        + _reasons_text(d)
        for d in skipped
    )
    how = "--dry-run" if dry_run else "the default"
    lines.append(f"nothing was pressed: this was a preview ({how})")
    lines.extend(guidance)
    return "\n".join(lines)


def _preview_guidance(
    args: argparse.Namespace, settings: Settings, device: Device
) -> list[str]:
    """How to switch from this preview to a pressing run; empty when none applies.

    A file that says press = true was overridden by --dry-run, so the switch
    is to remove that option (and add --out DIR unless the file has runs_dir)
    and nothing is printed. Otherwise the lines say what to add to the same
    command for this run (so its other options, such as -s, --deny and
    --config, stay as they were: --press, and --out DIR unless the file has
    runs_dir) and what makes every run press, and, unless the device answers
    that it is an emulator, --allow-real-device.
    """
    if settings.press:
        return []
    # These lines follow the run folder rule of _file_press_out; change both
    # together.
    add = "--press" if settings.runs_dir is not None else "--press --out DIR"
    this_time = f"add {add} to the same command"
    if args.dry_run:
        this_time = "remove --dry-run and " + this_time
    lines = [
        f"to press this time: {this_time}",
        "to always press: set press = true in"
        f" {_printable(_shown(str(settings.source)))}, and runs_dir (the folder"
        " that gets the run folders) or --out DIR on each run",
    ]
    try:
        emulator = device.is_emulator()
    except UiprobeError:
        emulator = False  # unknown: name the option, which is then needed
    if not emulator:
        lines.append("on a real device also add --allow-real-device")
    return lines


def _detail_json(detail: Detail) -> dict[str, object]:
    return dict(detail.to_dict())


def _preview_json(result: Preview, settings_path: str) -> dict[str, object]:
    screen = result.screen
    payload: dict[str, object] = {
        "schema": CRAWL_PREVIEW_SCHEMA,
        "package": result.package,
        "screen_id": screen.screen_id,
        "captured_at": screen.captured_at.isoformat(timespec="seconds"),
        "settings": settings_path,
        "pressed": False,
        "mode": "preview",
    }
    if result.foreground is not None:
        payload["foreground"] = result.foreground
    if result.keyboard_shown is not None:
        payload["keyboard_shown"] = result.keyboard_shown
    payload["press"] = [
        screen._element_dict_at(d.element, _decision_center(screen, d))  # noqa: SLF001
        for d in result.judgement.pressable
    ]
    skip: list[dict[str, object]] = []
    for decision in result.judgement.skipped:
        center = _decision_center(screen, decision)
        entry = screen._element_dict_at(decision.element, center)  # noqa: SLF001
        entry["reasons"] = [r for r in REASONS if r in decision.reasons]
        entry["details"] = [_detail_json(d) for d in decision.details]
        skip.append(entry)
    payload["skip"] = skip
    return payload


def _check_plan_stream(args: argparse.Namespace) -> None:
    """Refuse a pressing run whose plan cannot be shown (a closed stream)."""
    if (sys.stderr if args.json else sys.stdout) is None:
        stream = "stderr" if args.json else "stdout"
        msg = f"the plan cannot be shown: {stream} is closed, so nothing was pressed"
        raise _UsageError(msg)
