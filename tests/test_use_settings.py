"""Tests for the use of the settings file by ``crawl`` and ``key``.

Covers `policy_from_settings`, the new `Policy` fields, ``crawl`` (the
preview, which needs the file), ``key`` (which uses it when there is one) and the
``refused`` keyword of `Device.key`.
"""

from __future__ import annotations

import json
import random
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import El, FakeAndroid, Screen
from tests.test_crawl_preview import SCREENS, SERIAL, SHOP
from tests.test_guards import (
    DIALOG,
    _random_screen,
    el,
    make_screen,
    reasons,
    root,
    run,
)
from uiprobe import Device, Policy, cli, settings
from uiprobe.device import (
    DEVICE_WIDE_KEY_PREFIXES,
    DEVICE_WIDE_KEYS,
    FIXED_DEVICE_KEYS,
    RefusedKeys,
)
from uiprobe.guards import judge
from uiprobe.model import MIN_VISIBLE_PX
from uiprobe.settings import (
    SETTINGS_FILE,
    SettingsError,
    load_settings,
    policy_from_settings,
    render_default_settings,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SUFFIX = "; echo uiprobe-exit:$?"


def write(folder: Path, text: str, name: str = SETTINGS_FILE) -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def edited(old: str, new: str) -> str:
    text = render_default_settings()
    assert text.count(old) == 1, old
    return text.replace(old, new)


def loaded(tmp_path: Path, text: str | None = None) -> settings.Settings:
    return load_settings(write(tmp_path, text or render_default_settings()))


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty current folder."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def crawl_json(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> dict:
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json", *argv
    )
    assert (code, err) == (0, "")
    return json.loads(out)


def pressed(payload: dict) -> list[str]:
    return [e["text"] if "text" in e else e["id"] for e in payload["press"]]


def skipped(payload: dict) -> dict[str, list[str]]:
    return {e.get("text", e["id"]): e["reasons"] for e in payload["skip"]}


def sent_inputs(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c.startswith("input")]


# -- the new Policy fields ---------------------------------------------------------


def test_policy_defaults_for_the_new_fields() -> None:
    policy = Policy()
    assert policy.refuse_dialog_confirm is True
    assert policy.edges is None
    assert policy.min_visible_px == MIN_VISIBLE_PX


@pytest.mark.parametrize(
    "edges",
    [(1, 2, 3), (1, 2, 3, 4, 5), (-1, 0, 0, 0), (1, 2, 3, True), (1, 2, 3, 4.0)],
)
def test_policy_refuses_bad_edges(edges) -> None:
    with pytest.raises(ValueError, match="edges"):
        Policy(edges=edges)


@pytest.mark.parametrize("size", [0, -1, True, 8.0])
def test_policy_refuses_a_bad_min_visible_px(size) -> None:
    with pytest.raises(ValueError, match="min_visible_px"):
        Policy(min_visible_px=size)


def _dialog(question: str):
    text = el(
        "e9",
        text=question,
        cls="android.widget.TextView",
        flags=(),
        bounds=(30, 210, 290, 250),
    )
    return [text, el("e2", text="OK", bounds=(200, 380, 280, 420))]


def test_refuse_dialog_confirm_false_drops_only_that_reason() -> None:
    off = Policy(refuse_dialog_confirm=False)
    plain = _dialog("Are you sure?")
    assert reasons(run(plain, root_bounds=DIALOG), "e2") == ("dialog_confirm_button",)
    assert reasons(run(plain, policy=off, root_bounds=DIALOG), "e2") == ()
    denied = _dialog("Delete account?")
    assert reasons(run(denied, root_bounds=DIALOG), "e2") == (
        "confirm_button",
        "dialog_confirm_button",
    )
    assert reasons(run(denied, policy=off, root_bounds=DIALOG), "e2") == (
        "confirm_button",
    )


def test_edges_replace_the_api36_sizes_and_avoid_wins() -> None:
    item = el("e2", text="Item", bounds=(50, 0, 250, 30))  # center y 15
    assert reasons(run([item]), "e2") == ("screen_edge",)
    assert reasons(run([item], policy=Policy(edges=(0, 0, 0, 0))), "e2") == ()
    assert reasons(run([item], policy=Policy(edges=(16, 0, 0, 0))), "e2") == (
        "screen_edge",
    )
    assert reasons(run([item], policy=Policy(edges=(15, 0, 0, 0))), "e2") == ()
    # The edges would refuse the item, but an explicit avoid wins.
    both = Policy(edges=(16, 0, 0, 0), avoid=())
    assert reasons(run([item], policy=both), "e2") == ()
    side = el("e3", text="Side", bounds=(0, 300, 40, 340))  # center x 20
    assert reasons(run([side], policy=Policy(edges=(0, 0, 21, 0))), "e3") == (
        "screen_edge",
    )
    assert reasons(run([side], policy=Policy(edges=(0, 0, 20, 0))), "e3") == ()


def test_min_visible_px_changes_the_judging_only() -> None:
    thin = el("e2", text="Thin", bounds=(50, 100, 250, 105))  # 5 px high
    assert reasons(run([thin]), "e2") == ("not_visible",)
    assert reasons(run([thin], policy=Policy(min_visible_px=5)), "e2") == ()
    wide = el("e3", text="Wide", bounds=(50, 200, 250, 250))  # 50 px high
    assert reasons(run([wide], policy=Policy(min_visible_px=51)), "e3") == (
        "not_visible",
    )
    screen = make_screen([root(), thin])
    # Screen.visible_bounds keeps the fixed size whatever a policy says.
    assert screen.visible_bounds(screen.elements[1]) is None


# -- policy_from_settings -----------------------------------------------------------


def _same_decisions(first, second) -> bool:
    return all(
        (a.element.id, a.point, a.reasons, a.details)
        == (b.element.id, b.point, b.reasons, b.details)
        for a, b in zip(first.decisions, second.decisions, strict=True)
    ) and len(first.decisions) == len(second.decisions)


def test_default_settings_decide_like_the_default_policy(tmp_path) -> None:
    policy = policy_from_settings(loaded(tmp_path))
    rng = random.Random(20261120)  # noqa: S311
    pressable = skipped = outside = 0
    for _ in range(400):
        screen, _denies = _random_screen(rng)
        target = screen.package  # the screen is the target's; only the front varies
        for foreground in (target, "com.other"):
            expected = judge(screen, target, Policy(), foreground=foreground)
            actual = judge(screen, target, policy, foreground=foreground)
            assert _same_decisions(actual, expected)
            if foreground == target:
                pressable += len(expected.pressable)
                skipped += len(expected.skipped)
            else:
                outside += len(expected.skipped)
    # The cases must exercise real decisions, not only the outside-app refusal.
    assert pressable >= 20
    assert skipped > 100
    assert outside > 100
    for kids in _dialogs():
        expected = run(kids, root_bounds=DIALOG)
        actual = run(kids, policy=policy, root_bounds=DIALOG)
        assert _same_decisions(actual, expected)


def _dialogs():
    yield _dialog("Are you sure?")
    yield _dialog("Delete account?")
    yield [
        el("e2", text="7", bounds=(50, 100, 250, 150)),
        el(
            "e3",
            text="Sound",
            flags=("clickable", "checkable"),
            bounds=(50, 200, 250, 250),
        ),
        el("e4", bounds=(50, 300, 250, 340)),
        el("e5", text="Top", bounds=(50, 0, 250, 30)),
        el("e6", text="Thin", bounds=(50, 400, 250, 405)),
        el("e7", text="Side", bounds=(0, 500, 40, 540)),
        el("e8", text="Bottom", bounds=(50, 600, 250, 640)),
    ]


def test_the_default_policy_equals_the_default_file_policy(tmp_path) -> None:
    policy = policy_from_settings(loaded(tmp_path))
    assert policy.deny_words == Policy().deny_words
    assert policy.confirm_words == Policy().confirm_words
    assert policy.edges == (24, 32, 30, 30)
    assert replace(policy, edges=None) == Policy()


def test_extra_deny_and_tap_unlabeled(tmp_path) -> None:
    loaded_settings = loaded(tmp_path)
    policy = policy_from_settings(
        loaded_settings, extra_deny=["Zebra", "Quux"], tap_unlabeled=True
    )
    assert policy.deny_words[-2:] == ("Zebra", "Quux")
    assert policy.tap_unlabeled is True
    assert policy_from_settings(loaded_settings).tap_unlabeled is False
    on = loaded(tmp_path, edited("tap_unlabeled = false", "tap_unlabeled = true"))
    assert policy_from_settings(on).tap_unlabeled is True
    assert policy_from_settings(on, tap_unlabeled=False).tap_unlabeled is False


def test_extra_deny_is_checked(tmp_path) -> None:
    loaded_settings = loaded(tmp_path)
    with pytest.raises(TypeError, match="not one string"):
        policy_from_settings(loaded_settings, extra_deny="rent")
    with pytest.raises(ValueError, match="empty"):
        policy_from_settings(loaded_settings, extra_deny=["  "])


@pytest.mark.parametrize(
    ("old", "new", "field", "value"),
    [
        (
            "skip_number_only = true",
            "skip_number_only = false",
            "skip_number_only",
            False,
        ),
        ("skip_toggles = true", "skip_toggles = false", "skip_toggles", False),
        (
            "refuse_dialog_confirm = true",
            "refuse_dialog_confirm = false",
            "refuse_dialog_confirm",
            False,
        ),
        ("tap_unlabeled = false", "tap_unlabeled = true", "tap_unlabeled", True),
        ("min_visible_px = 8", "min_visible_px = 3", "min_visible_px", 3),
        ("edge_top_px = 24", "edge_top_px = 10", "edges", (10, 32, 30, 30)),
        ("edge_bottom_px = 32", "edge_bottom_px = 0", "edges", (24, 0, 30, 30)),
        ("edge_side_px = 30", "edge_side_px = 5", "edges", (24, 32, 5, 5)),
    ],
)
def test_each_setting_changes_only_its_field(tmp_path, old, new, field, value) -> None:
    policy = policy_from_settings(loaded(tmp_path, edited(old, new)))
    default = policy_from_settings(loaded(tmp_path))
    assert getattr(policy, field) == value
    assert replace(policy, **{field: getattr(default, field)}) == default


def test_a_removed_deny_word_is_not_applied(tmp_path) -> None:
    text = render_default_settings().replace(
        "remove = []", 'remove = ["delete", "account"]', 1
    )
    policy = policy_from_settings(loaded(tmp_path, text))
    assert "delete" not in policy.deny_words
    item = el("e2", text="Delete", bounds=(50, 100, 250, 150))
    assert reasons(run([item]), "e2") == ("deny_word",)
    assert reasons(run([item], policy=policy), "e2") == ()


def test_the_generated_file_lists_the_start_over_words(tmp_path) -> None:
    text = render_default_settings()
    for word in ("restart", "undo", "やり直", "元に戻"):
        assert f'"{word}"' in text
        assert word in loaded(tmp_path, text).deny_words


def test_removing_the_start_over_words_makes_them_pressable_again(tmp_path) -> None:
    text = render_default_settings().replace(
        "remove = []", 'remove = ["restart", "undo", "やり直", "元に戻"]', 1
    )
    policy = policy_from_settings(loaded(tmp_path, text))
    for label in ("Restart", "Undo", "やり直す", "元に戻す"):
        item = el("e2", text=label, bounds=(50, 100, 250, 150))
        assert reasons(run([item]), "e2") == ("deny_word",)
        assert reasons(run([item], policy=policy), "e2") == ()
    screen = [
        el(
            "e2",
            text="Are you sure you want to restart?",
            cls="android.widget.TextView",
            flags=(),
            bounds=(20, 100, 300, 140),
        ),
        el("e3", text="Yes I'm sure!", bounds=(20, 300, 300, 360)),
    ]
    assert reasons(run(screen), "e3") == ("confirm_button",)
    assert reasons(run(screen, policy=policy), "e3") == ()


# -- crawl needs the settings file -----------------------------------------


def test_crawl_with_the_default_file_gives_the_old_preview(
    capsys, fake_adb, android, folder
) -> None:
    write(folder, render_default_settings())
    payload = crawl_json(capsys, fake_adb)
    assert payload["settings"] == SETTINGS_FILE
    assert pressed(payload) == ["Browse", "Help"]
    assert skipped(payload) == {
        "Delete account": ["deny_word"],
        "Open row": ["covered", "deny_word"],
        "e6": ["unlabeled"],
        "Settings": ["disabled"],
        "Offscreen": ["not_visible"],
    }
    assert android.inputs == []


def test_crawl_without_a_file_stops_before_any_contact(
    capsys, fake_adb, android, folder
) -> None:
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP)
    assert (code, out) == (2, "")
    assert err == (
        "uiprobe: error: crawl needs a settings file and none was found"
        " (looked for ./uiprobe.toml)\n"
        "create it with:    uiprobe init\n"
        f"or name one with:  uiprobe crawl {SHOP} --config PATH\n"
    )
    assert fake_adb.requests == []
    assert android.inputs == []
    assert list(folder.iterdir()) == []


@pytest.mark.usefixtures("android", "folder")
def test_crawl_without_a_file_in_json(capsys, fake_adb) -> None:
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--json")
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert error["message"].splitlines() == [
        "crawl needs a settings file and none was found (looked for ./uiprobe.toml)",
        "create it with:    uiprobe init",
        f"or name one with:  uiprobe crawl {SHOP} --config PATH",
    ]
    assert error["details"] == {
        "looked_for": "./uiprobe.toml",
        "create": "uiprobe init",
        "name_one": f"uiprobe crawl {SHOP} --config PATH",
    }
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_the_folder_is_not_the_only_place_that_counts(
    capsys, fake_adb, tmp_path
) -> None:
    # No user folder or parent folder is searched.
    write(tmp_path, render_default_settings())
    code, _, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run")
    assert code == 2
    assert "none was found" in err
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_crawl_config_names_another_file(capsys, fake_adb, folder, tmp_path) -> None:
    other = write(
        tmp_path, edited("skip_toggles = true", "skip_toggles = false"), "o.toml"
    )
    payload = crawl_json(capsys, fake_adb, "--config", str(other))
    assert payload["settings"] == str(other)
    assert list(folder.iterdir()) == []


@pytest.mark.usefixtures("android")
def test_crawl_config_wins_over_the_file_in_the_folder(
    capsys, fake_adb, folder, tmp_path
) -> None:
    write(folder, render_default_settings())
    text = edited("edge_top_px = 24", "edge_top_px = 126")
    other = write(tmp_path, text, "o.toml")
    assert skipped(crawl_json(capsys, fake_adb, "--config", str(other)))["Browse"] == [
        "screen_edge"
    ]
    assert "Browse" in pressed(crawl_json(capsys, fake_adb))


@pytest.mark.usefixtures("android", "folder")
def test_crawl_text_shows_the_settings_path(capsys, fake_adb, tmp_path) -> None:
    other = write(tmp_path, render_default_settings(), "o.toml")
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(other)
    )
    assert (code, err) == (0, "")
    assert out.splitlines()[1] == f"settings: {other}"


@pytest.mark.usefixtures("android", "folder")
def test_crawl_config_that_is_missing_gets_the_guidance(
    capsys, fake_adb, tmp_path
) -> None:
    path = tmp_path / "missing.toml"
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(path)
    )
    assert (code, out) == (2, "")
    assert err == (
        "uiprobe: error: crawl needs a settings file and none was found"
        f" (looked for {path})\n"
        f"create it with:    {cli._create_line(str(path))}\n"
        f"or name one with:  uiprobe crawl {SHOP} --dry-run --config PATH\n"
    )
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_crawl_config_that_is_missing_in_json(capsys, fake_adb, tmp_path) -> None:
    path = tmp_path / "missing.toml"
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json", "--config", str(path)
    )
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert error["message"].splitlines()[0] == (
        f"crawl needs a settings file and none was found (looked for {path})"
    )
    assert error["details"] == {
        "looked_for": str(path),
        "create": cli._create_line(str(path)),
        "name_one": f"uiprobe crawl {SHOP} --dry-run --config PATH",
    }
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_the_missing_file_hint_works_with_a_press_file(
    capsys, fake_adb, tmp_path
) -> None:
    path = tmp_path / "missing.toml"
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json", "--config", str(path)
    )
    assert (code, err) == (2, "")
    hint = json.loads(out)["error"]["details"]["name_one"]
    # Follow the printed command with a valid file that says press = true.
    pressing = write(
        tmp_path,
        render_default_settings().replace("press = false", "press = true"),
        "press.toml",
    )
    argv = [str(pressing) if word == "PATH" else word for word in hint.split()]
    assert argv[:2] == ["uiprobe", "crawl"]
    code, _, err = invoke(capsys, fake_adb, *argv[1:], "--json")
    assert code == 0
    assert err == ""
    # Without --dry-run the same hint is refused.
    code, out, _ = invoke(capsys, fake_adb, "crawl", SHOP, "--config", str(pressing))
    assert code == 2


@pytest.mark.usefixtures("android", "folder")
def test_crawl_config_below_a_file_gets_the_guidance(
    capsys, fake_adb, tmp_path
) -> None:
    plain = write(tmp_path, "x", "plain.txt")
    path = plain / "inner.toml"  # NotADirectoryError, not FileNotFoundError
    code, _, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(path)
    )
    assert code == 2
    assert f"none was found (looked for {path})" in err
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_crawl_config_that_is_a_broken_link_stays_a_settings_error(
    capsys, fake_adb, tmp_path
) -> None:
    link = tmp_path / "link.toml"
    try:
        link.symlink_to(tmp_path / "nowhere.toml")
    except OSError:  # pragma: no cover - no symlinks here
        pytest.skip("symbolic links are not available")
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(link)
    )
    assert (code, out) == (2, "")
    assert "none was found" not in err
    assert "create it with" not in err
    assert err.startswith(f"uiprobe: error: {link}: ")
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_crawl_config_that_is_a_directory_stays_a_settings_error(
    capsys, fake_adb, tmp_path
) -> None:
    code, _, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(tmp_path)
    )
    assert code == 2
    assert "not a regular file" in err
    assert "none was found" not in err
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_a_settings_error_stops_the_crawl(capsys, fake_adb, folder) -> None:
    write(folder, edited("min_visible_px = 8", "min_visible_px = 0"))
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json")
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert "min_visible_px" in error["message"]
    assert SETTINGS_FILE in error["message"]
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_a_directory_named_like_the_file_is_a_settings_error(
    capsys, fake_adb, folder
) -> None:
    (folder / SETTINGS_FILE).mkdir()
    code, _, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run")
    assert code == 2
    assert "not a regular file" in err
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_settings_warnings_go_to_stderr(capsys, fake_adb, folder) -> None:
    text = render_default_settings()
    head, _, tail = text.rpartition("remove = []")
    write(folder, head + 'remove = ["KEYCODE_POWER"]' + tail)
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json")
    assert code == 0
    assert json.loads(out)["schema"] == "uiprobe.crawl_preview/1"
    assert err.startswith("uiprobe: warning: [keys] remove lists KEYCODE_POWER")


@pytest.mark.usefixtures("android")
def test_removing_a_deny_word_in_the_file_makes_the_element_pressable(
    capsys, fake_adb, folder
) -> None:
    write(
        folder,
        render_default_settings().replace(
            "remove = []", 'remove = ["delete", "account"]', 1
        ),
    )
    payload = crawl_json(capsys, fake_adb)
    assert "Delete account" in pressed(payload)
    assert "Browse" in pressed(payload)


@pytest.mark.usefixtures("android")
def test_a_later_default_word_applies_without_being_listed(
    capsys, fake_adb, folder, monkeypatch
) -> None:
    write(folder, render_default_settings())  # a file of the current version
    later = settings.DEFAULTS_VERSION + 1
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", later)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (later, "browse"))
    )
    payload = crawl_json(capsys, fake_adb)
    assert skipped(payload)["Browse"] == ["deny_word"]
    assert pressed(payload) == ["Help"]


@pytest.mark.usefixtures("android")
def test_deny_still_adds_words_for_the_run(capsys, fake_adb, folder) -> None:
    write(
        folder,
        render_default_settings().replace(
            "remove = []", 'remove = ["delete", "account"]', 1
        ),
    )
    payload = crawl_json(capsys, fake_adb, "--deny", "browse")
    assert "Browse" not in pressed(payload)
    assert skipped(payload)["Browse"] == ["deny_word"]
    assert "Delete account" in pressed(payload)
    assert load_settings(folder / SETTINGS_FILE).deny_words.count("browse") == 0


@pytest.mark.usefixtures("android")
def test_deny_file_is_rejected(capsys, fake_adb, folder, tmp_path) -> None:
    write(folder, render_default_settings())
    words = write(tmp_path, "browse\n", "deny.txt")
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--deny-file", str(words)
    )
    assert (code, out) == (2, "")
    assert "--deny-file" in err
    assert fake_adb.requests == []


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("skip_number_only = true", "skip_number_only = false", "number_only"),
        ("skip_toggles = true", "skip_toggles = false", "toggle"),
    ],
)
def test_a_rule_turned_off_changes_only_its_reason(
    capsys, fake_adb, android, folder, old, new, reason
) -> None:
    android.show("rules")
    write(folder, render_default_settings())
    before = skipped(crawl_json(capsys, fake_adb))
    write(folder, edited(old, new))
    after = skipped(crawl_json(capsys, fake_adb))
    for text, found in before.items():
        assert after.get(text, []) == [r for r in found if r != reason]


@pytest.mark.usefixtures("android")
def test_tap_unlabeled_in_the_file_and_on_the_command_line(
    capsys, fake_adb, folder
) -> None:
    write(folder, render_default_settings())
    off = crawl_json(capsys, fake_adb)
    assert skipped(off)["e6"] == ["unlabeled"]
    forced = crawl_json(capsys, fake_adb, "--tap-unlabeled")
    assert "e6" not in skipped(forced)
    write(folder, edited("tap_unlabeled = false", "tap_unlabeled = true"))
    from_file = crawl_json(capsys, fake_adb)
    assert pressed(from_file) == pressed(forced)


@pytest.mark.usefixtures("android")
def test_edge_sizes_and_min_visible_px_come_from_the_file(
    capsys, fake_adb, folder
) -> None:
    write(folder, edited("edge_top_px = 24", "edge_top_px = 126"))
    payload = crawl_json(capsys, fake_adb)
    assert skipped(payload)["Browse"] == ["screen_edge"]
    assert pressed(payload) == ["Help"]
    write(folder, edited("min_visible_px = 8", "min_visible_px = 64"))
    payload = crawl_json(capsys, fake_adb)
    assert payload["press"] == []
    assert skipped(payload)["Browse"] == ["not_visible"]


# -- key -------------------------------------------------------------------------------


@pytest.mark.usefixtures("android", "folder")
def test_key_without_a_file_uses_the_built_in_lists(capsys, fake_adb) -> None:
    code, out, err = invoke(capsys, fake_adb, "key", "KEYCODE_VOLUME_UP", "--json")
    assert (code, err) == (2, "")
    assert "acts on the whole device" in json.loads(out)["error"]["message"]
    assert fake_adb.requests == []
    code, out, _ = invoke(capsys, fake_adb, "key", "back")
    assert (code, out) == (0, "pressed key KEYCODE_BACK\n")


def test_key_with_a_file_that_removes_volume_up_sends_it(
    capsys, fake_adb, android, folder
) -> None:
    text = render_default_settings()
    head, _, tail = text.rpartition("remove = []")
    write(folder, head + 'remove = ["KEYCODE_VOLUME_UP"]' + tail)
    code, out, err = invoke(capsys, fake_adb, "key", "KEYCODE_VOLUME_UP")
    assert (code, out, err) == (0, "pressed key KEYCODE_VOLUME_UP\n", "")
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_VOLUME_UP" + SUFFIX]
    assert [(i.kind, i.args) for i in android.inputs] == [
        ("key", ("KEYCODE_VOLUME_UP",))
    ]


def test_key_with_a_file_still_refuses_a_fixed_key(
    capsys, fake_adb, android, folder
) -> None:
    text = render_default_settings()
    head, _, tail = text.rpartition("remove = []")
    write(folder, head + 'remove = ["KEYCODE_POWER"]' + tail)
    code, out, err = invoke(capsys, fake_adb, "key", "KEYCODE_POWER", "--json")
    assert code == 2
    assert "acts on the whole device" in json.loads(out)["error"]["message"]
    assert err.startswith("uiprobe: warning: [keys] remove lists KEYCODE_POWER")
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_key_with_a_file_refuses_what_the_file_lists(capsys, fake_adb, folder) -> None:
    text = render_default_settings()
    head, _, tail = text.partition("refuse = [")
    write(folder, head + 'refuse = [\n    "KEYCODE_F1",' + tail)
    code, out, _ = invoke(capsys, fake_adb, "key", "KEYCODE_F1", "--json")
    assert code == 2
    message = json.loads(out)["error"]["message"]
    assert message == (
        f"KEYCODE_F1 is refused by {SETTINGS_FILE}; remove it there to send it"
    )
    assert fake_adb.requests == []
    code, _, _ = invoke(capsys, fake_adb, "key", "KEYCODE_F2")
    assert code == 0


@pytest.mark.usefixtures("android")
def test_key_refused_by_a_prefix_names_the_prefix(capsys, fake_adb, folder) -> None:
    write(folder, render_default_settings())
    code, out, _ = invoke(capsys, fake_adb, "key", "KEYCODE_MEDIA_PLAY", "--json")
    assert code == 2
    message = json.loads(out)["error"]["message"]
    assert message == (
        f"KEYCODE_MEDIA_PLAY is refused because {SETTINGS_FILE} lists the prefix"
        " KEYCODE_MEDIA_; changing that prefix affects every key that starts with it"
    )
    assert "remove it there" not in message
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_key_config_names_a_file_and_wins(capsys, fake_adb, folder, tmp_path) -> None:
    write(folder, render_default_settings())
    text = render_default_settings()
    head, _, tail = text.rpartition("remove = []")
    other = write(tmp_path, head + 'remove = ["KEYCODE_VOLUME_UP"]' + tail, "o.toml")
    code, out, _ = invoke(
        capsys, fake_adb, "key", "--config", str(other), "KEYCODE_VOLUME_UP"
    )
    assert (code, out) == (0, "pressed key KEYCODE_VOLUME_UP\n")
    code, _, _ = invoke(capsys, fake_adb, "key", "KEYCODE_VOLUME_UP")
    assert code == 2


def test_key_config_errors_are_usage_errors(
    capsys, fake_adb, android, folder, tmp_path
) -> None:
    missing = tmp_path / "missing.toml"
    code, out, err = invoke(capsys, fake_adb, "key", "--config", str(missing), "back")
    assert (code, out) == (2, "")
    assert err == f"uiprobe: error: {missing}: no such file\n"
    write(folder, "not toml = = =")
    code, _, err = invoke(capsys, fake_adb, "key", "back")
    assert code == 2
    assert "could not be parsed" in err
    assert fake_adb.requests == []
    assert android.inputs == []


# -- Device.key(refused=...) ---------------------------------------------------


def device_of(fake_adb: FakeAdbServer) -> Device:
    return Device(adb_server=fake_adb.address)


@pytest.mark.usefixtures("android")
def test_device_key_with_refused_keys(fake_adb) -> None:
    device = device_of(fake_adb)
    refused = RefusedKeys(frozenset({"KEYCODE_F1"}), ("KEYCODE_MEDIA_",))
    assert device.key("KEYCODE_VOLUME_UP", refused=refused) == "KEYCODE_VOLUME_UP"
    before = len(fake_adb.requests)
    for name, text in (
        ("KEYCODE_F1", "the refused-keys list"),
        ("KEYCODE_MEDIA_PLAY", "the refused-keys list"),
        ("KEYCODE_POWER", "whole device"),
        ("KEYCODE_CALL", "whole device"),
    ):
        with pytest.raises(ValueError, match=text):
            device.key(name, refused=refused)
    assert len(fake_adb.requests) == before
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_VOLUME_UP" + SUFFIX]


@pytest.mark.usefixtures("android")
def test_a_key_refused_by_the_file_says_so(fake_adb) -> None:
    device = device_of(fake_adb)
    refused = RefusedKeys(frozenset({"KEYCODE_F1"}), ("KEYCODE_MEDIA_",))
    exact = "KEYCODE_F1 is refused by the refused-keys list; remove it there to send it"
    with pytest.raises(ValueError, match=f"^{re.escape(exact)}$"):
        device.key("KEYCODE_F1", refused=refused)
    prefix = (
        "KEYCODE_MEDIA_PLAY is refused because the refused-keys list lists the prefix"
        " KEYCODE_MEDIA_; changing that prefix affects every key that starts with it"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(prefix)}$"):
        device.key("KEYCODE_MEDIA_PLAY", refused=refused)


@pytest.mark.usefixtures("android")
def test_refused_keys_source_is_used_in_the_message(fake_adb) -> None:
    device = device_of(fake_adb)
    refused = RefusedKeys(
        frozenset({"KEYCODE_F1", "KEYCODE_MEDIA_STOP"}),
        ("KEYCODE_MEDIA_",),
        "my_file",
    )
    with pytest.raises(ValueError, match="KEYCODE_F1 is refused by my_file; remove"):
        device.key("KEYCODE_F1", refused=refused)
    with pytest.raises(ValueError, match="because my_file lists the prefix") as info:
        device.key("KEYCODE_MEDIA_PLAY", refused=refused)
    assert "also listed" not in str(info.value)
    # Listed by name as well: removing the prefix alone would not help.
    with pytest.raises(ValueError, match="KEYCODE_MEDIA_STOP is also listed by name"):
        device.key("KEYCODE_MEDIA_STOP", refused=refused)
    with pytest.raises(TypeError, match="source"):
        RefusedKeys(frozenset(), (), 3)  # ty: ignore[invalid-argument-type]


@pytest.mark.usefixtures("android")
def test_a_fixed_key_keeps_the_whole_device_message(fake_adb) -> None:
    device = device_of(fake_adb)
    # Listed in the file as well, it is still fixed and cannot be removed.
    refused = RefusedKeys(frozenset({"KEYCODE_POWER"}), ())
    message = (
        "KEYCODE_POWER acts on the whole device, not on the app in front; "
        "uiprobe does not send it"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        device.key("KEYCODE_POWER", refused=refused)


@pytest.mark.usefixtures("android")
def test_a_built_in_refusal_keeps_its_message(fake_adb) -> None:
    device = device_of(fake_adb)
    message = (
        "KEYCODE_VOLUME_UP acts on the whole device, not on the app in front; "
        "uiprobe does not send it"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        device.key("KEYCODE_VOLUME_UP")


@pytest.mark.usefixtures("android")
def test_an_empty_refused_set_still_refuses_the_fixed_keys(fake_adb) -> None:
    device = device_of(fake_adb)
    nothing = RefusedKeys(frozenset(), ())
    for name in FIXED_DEVICE_KEYS:
        with pytest.raises(ValueError, match="whole device"):
            device.key(name, refused=nothing)
    assert sent_inputs(fake_adb) == []
    assert device.key("KEYCODE_VOLUME_UP", refused=nothing) == "KEYCODE_VOLUME_UP"


@pytest.mark.usefixtures("android")
def test_device_key_without_the_keyword_uses_the_built_in_lists(fake_adb) -> None:
    device = device_of(fake_adb)
    with pytest.raises(ValueError, match="whole device"):
        device.key("KEYCODE_VOLUME_UP")
    with pytest.raises(ValueError, match="whole device"):
        device.key("KEYCODE_MEDIA_PLAY")
    assert device.key("KEYCODE_F1", refused=None) == "KEYCODE_F1"
    built_in = RefusedKeys(DEVICE_WIDE_KEYS, DEVICE_WIDE_KEY_PREFIXES)
    with pytest.raises(ValueError, match="the refused-keys list"):
        device.key("KEYCODE_VOLUME_UP", refused=built_in)


@pytest.mark.usefixtures("android")
def test_the_refused_keyword_is_checked(fake_adb) -> None:
    device = device_of(fake_adb)
    before = len(fake_adb.requests)
    with pytest.raises(TypeError, match="RefusedKeys"):
        device.key("back", refused=("KEYCODE_F1",))  # ty: ignore[invalid-argument-type]
    assert len(fake_adb.requests) == before


@pytest.mark.parametrize("bad", ["", "keycode_f1", "KEYCODE_", "KEYCODE_" + "A" * 41])
def test_refused_keys_check_their_names(bad) -> None:
    with pytest.raises(ValueError, match="not a key name"):
        RefusedKeys(frozenset({bad}), ())
    with pytest.raises(ValueError, match="not a key name"):
        RefusedKeys(frozenset(), (bad,))


def test_refused_keys_refuse_non_strings() -> None:
    with pytest.raises(TypeError, match="str"):
        RefusedKeys(frozenset({4}), ())  # ty: ignore[invalid-argument-type]


def test_the_settings_lists_make_refused_keys(tmp_path) -> None:
    loaded_settings = loaded(tmp_path)
    refused = RefusedKeys(
        frozenset(loaded_settings.refused_keys), loaded_settings.refused_key_prefixes
    )
    assert refused.keys == DEVICE_WIDE_KEYS
    assert refused.prefixes == DEVICE_WIDE_KEY_PREFIXES


def test_settings_error_is_what_the_reader_raises(tmp_path) -> None:
    with pytest.raises(SettingsError):
        load_settings(tmp_path / "missing.toml")


def test_a_settings_file_of_exactly_one_mib_is_read(tmp_path) -> None:
    text = render_default_settings()
    padding = 1024 * 1024 - len(text.encode()) - len("\n# ")
    path = write(tmp_path, text + "\n# " + "x" * padding)
    assert path.stat().st_size == 1024 * 1024
    assert load_settings(path).deny_words == loaded(tmp_path).deny_words
    over = write(tmp_path, text + "\n# " + "x" * (padding + 1), "over.toml")
    with pytest.raises(SettingsError, match="larger than"):
        load_settings(over)


def test_confirm_words_come_from_the_file(tmp_path) -> None:
    text = render_default_settings()
    head, marker, tail = text.partition("[confirm]")
    section, sep, after = tail.partition("remove = []")
    assert sep
    custom = head + marker + section + 'remove = ["OK"]' + after
    policy = policy_from_settings(loaded(tmp_path, custom))
    default = policy_from_settings(loaded(tmp_path))
    assert len(policy.confirm_words) == len(default.confirm_words) - 1
    assert "OK" in default.confirm_words
    assert "OK" not in policy.confirm_words
    # A deny word on the screen and an "OK" button: only the file that keeps
    # "ok" as a confirm word gives the confirm_button reason.
    screen = _dialog("Delete account?")
    off = {"refuse_dialog_confirm": False}
    assert reasons(
        run(screen, policy=replace(default, **off), root_bounds=DIALOG), "e2"
    ) == ("confirm_button",)
    assert (
        reasons(run(screen, policy=replace(policy, **off), root_bounds=DIALOG), "e2")
        == ()
    )


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("kind", ["loop", "dangling"])
def test_a_link_or_entry_that_cannot_be_read_is_not_missing(
    capsys, fake_adb, folder, tmp_path, kind
) -> None:
    link = folder / SETTINGS_FILE
    try:
        if kind == "loop":
            link.symlink_to(link.name)
        else:
            link.symlink_to(tmp_path / "nowhere.toml")
    except OSError:
        pytest.skip("cannot create symlinks here")
    for argv in (("crawl", SHOP, "--dry-run"), ("key", "KEYCODE_F2")):
        code, out, err = invoke(capsys, fake_adb, *argv)
        assert (code, out) == (2, "")
        assert "none was found" not in err
        assert str(SETTINGS_FILE) in err
        if kind == "dangling":
            assert "the link's target is missing" in err
        else:
            assert "target is missing" not in err
        assert fake_adb.requests == []


def test_too_large_screen_center_uses_the_fixed_visible_size() -> None:
    # 5000 buttons plus the root is over the element limit; the preview shows the
    # center from Screen.visible_bounds (8 px) although min_visible_px is 20.
    small = el("s1", text="Small", bounds=(100, 100, 112, 112))
    buttons = [el(f"b{i}", text="Item") for i in range(5000)]
    screen = make_screen([root(), small, *buttons])
    policy = Policy(min_visible_px=20)
    found = judge(screen, screen.package, policy, foreground=screen.package)
    decision = next(d for d in found.decisions if d.element.id == "s1")
    assert decision.reasons == ("screen_too_large",)
    assert decision.point is None
    assert cli._decision_center(screen, decision) == (106, 106)
    thin = el("s2", text="Thin", bounds=(100, 200, 112, 204))
    screen = make_screen([root(), thin, *buttons])
    found = judge(screen, screen.package, policy, foreground=screen.package)
    decision = next(d for d in found.decisions if d.element.id == "s2")
    assert cli._decision_center(screen, decision) is None


@pytest.mark.usefixtures("android", "folder")
@pytest.mark.parametrize(
    ("given", "create"),
    [
        ("sub/my.toml", "uiprobe init sub/my.toml"),
        ("my dir/my.toml", "uiprobe init PATH"),
    ],
)
def test_crawl_missing_config_create_line_names_the_path(
    capsys, fake_adb, given, create
) -> None:
    args = ("crawl", SHOP, "--dry-run", "--config", given)
    code, out, err = invoke(capsys, fake_adb, *args)
    assert (code, out) == (2, "")
    assert err.splitlines()[1] == f"create it with:    {create}"
    code, out, err = invoke(capsys, fake_adb, *args, "--json")
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert error["message"].splitlines()[1] == f"create it with:    {create}"
    assert error["details"]["create"] == create
    assert fake_adb.requests == []


@pytest.mark.parametrize(
    ("given", "windows", "create"),
    [
        (
            "C:\\Users\\me\\uiprobe.toml",
            True,
            "uiprobe init C:\\Users\\me\\uiprobe.toml",
        ),
        ("C:\\Program Files\\x.toml", True, "uiprobe init PATH"),
        ("C:\\Users\\me\\uiprobe.toml", False, "uiprobe init PATH"),
        ("-x.toml", True, "uiprobe init PATH"),
        ("sub/my.toml", True, "uiprobe init sub/my.toml"),
    ],
)
def test_create_line_follows_the_platform_rule(
    monkeypatch, given, windows, create
) -> None:
    monkeypatch.setattr(cli, "_ON_WINDOWS", windows)
    assert cli._create_line(given) == create


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize(
    "command", [("crawl", SHOP, "--dry-run"), ("key", "KEYCODE_F2")]
)
@pytest.mark.parametrize("as_json", [False, True])
def test_config_ending_with_a_separator_is_a_usage_error(
    capsys, fake_adb, folder, command, as_json
) -> None:
    write(folder, render_default_settings(), "rules.toml")
    extra = ("--json",) if as_json else ()
    code, out, err = invoke(
        capsys, fake_adb, *command, "--config", "rules.toml/", *extra
    )
    assert code == 2
    text = json.loads(out)["error"]["message"] if as_json else err
    assert "rules.toml/" in text
    assert "path separator" in text
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_a_path_with_a_newline_is_escaped_in_text_and_kept_in_json(
    capsys, fake_adb, tmp_path
) -> None:
    try:
        other = write(tmp_path, render_default_settings(), "a\nb.toml")
    except OSError:
        pytest.skip("cannot create such a file name here")
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(other)
    )
    assert (code, err) == (0, "")
    line = out.splitlines()[1]
    assert line == "settings: " + str(other).replace("\n", "\\x0a")
    assert "\\x0a" in line
    assert crawl_json(capsys, fake_adb, "--config", str(other))["settings"] == str(
        other
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="a Windows file name cannot hold a newline"
)
@pytest.mark.usefixtures("android", "folder")
def test_a_missing_path_with_a_newline_is_escaped_in_the_guidance(
    capsys, fake_adb, tmp_path
) -> None:
    given = str(tmp_path / "a\nb.toml")
    shown = given.replace("\n", "\\x0a")
    args = ("crawl", SHOP, "--dry-run", "--config", given)
    code, out, err = invoke(capsys, fake_adb, *args)
    assert (code, out) == (2, "")
    assert err.splitlines()[0] == (
        f"uiprobe: error: crawl needs a settings file and none was found"
        f" (looked for {shown})"
    )
    assert len(err.splitlines()) == 3
    code, out, err = invoke(capsys, fake_adb, *args, "--json")
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert error["details"]["looked_for"] == given
    assert error["message"].splitlines()[0].endswith(f"(looked for {shown})")
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android", "folder")
def test_key_refusal_escapes_a_path_with_a_newline(capsys, fake_adb, tmp_path) -> None:
    try:
        other = write(tmp_path, render_default_settings(), "a\nb.toml")
    except OSError:
        pytest.skip("cannot create such a file name here")
    code, out, err = invoke(
        capsys, fake_adb, "key", "KEYCODE_VOLUME_UP", "--config", str(other)
    )
    assert (code, out) == (2, "")
    assert str(other).replace("\n", "\\x0a") in err
    assert len(err.splitlines()) == 1
    assert fake_adb.requests == []


# -- irreversible screens through the file --------------------------------------


def _warned_screen():
    text = "android.widget.TextView"
    return [
        el(
            "e2",
            text="Restart game? You can't undo this",
            cls=text,
            flags=(),
            bounds=(20, 100, 300, 140),
        ),
        el("e3", text="Yes I'm sure!", bounds=(20, 300, 300, 360)),
        el("e4", text="Wait, cancel!", bounds=(20, 380, 300, 440)),
    ]


def test_the_default_file_refuses_every_element_of_a_warned_screen(tmp_path):
    policy = policy_from_settings(loaded(tmp_path))
    assert policy.skip_irreversible is True
    found = run(_warned_screen(), policy=policy)
    assert reasons(found, "e3") == ("irreversible_screen", "confirm_button")
    assert reasons(found, "e4") == ("irreversible_screen",)


def test_the_switch_in_the_file_turns_the_rule_off(tmp_path):
    text = render_default_settings().replace(
        "refuse_irreversible_screens = true", "refuse_irreversible_screens = false"
    )
    policy = policy_from_settings(loaded(tmp_path, text))
    assert policy.skip_irreversible is False
    found = run(_warned_screen(), policy=policy)
    assert reasons(found, "e3") == ("confirm_button",)
    assert reasons(found, "e4") == ()


def test_phrases_removed_in_the_file_stop_applying(tmp_path):
    text = render_default_settings()
    head, _, tail = text.partition("[irreversible]")
    table, _, rest = tail.partition("[rules]")
    table = table.replace("remove = []", 'remove = ["can\'t undo", "CANNOT UNDO"]')
    policy = policy_from_settings(
        loaded(tmp_path, head + "[irreversible]" + table + "[rules]" + rest)
    )
    assert "can't undo" not in policy.irreversible_phrases
    found = run(_warned_screen(), policy=policy)
    assert reasons(found, "e3") == ("confirm_button",)
    assert reasons(found, "e4") == ()
    other = [
        el("e2", text="Cannot be undone", cls="android.widget.TextView", flags=()),
        el("e3", text="Home", bounds=(20, 300, 300, 360)),
    ]
    assert reasons(run(other, policy=policy), "e3") == ("irreversible_screen",)


def test_japanese_phrases_come_through_the_file(tmp_path):
    policy = policy_from_settings(loaded(tmp_path))
    screen = [
        el(
            "e2",
            text="この操作は取り消せません",
            cls="android.widget.TextView",
            flags=(),
        ),
        el("e3", text="ホーム", bounds=(20, 300, 300, 360)),
    ]
    assert reasons(run(screen, policy=policy), "e3") == ("irreversible_screen",)


def test_the_default_file_decides_like_policy_on_random_screens(tmp_path):
    rng = random.Random(20261122)  # noqa: S311
    policy = policy_from_settings(loaded(tmp_path))
    for _ in range(100):
        screen, _denies = _random_screen(rng)
        assert judge(
            screen, "com.example.shop", policy, foreground="com.example.shop"
        ) == judge(screen, "com.example.shop", Policy(), foreground="com.example.shop")


# -- a settings file written before defaults version 2 ------------------------------


def _v1_text() -> str:
    path = Path(__file__).parent / "data" / "settings_v1.toml"
    text = path.read_text(encoding="utf-8")
    assert "defaults_version = 1\n" in text
    assert "[irreversible]" not in text
    return text


@pytest.mark.usefixtures("android")
def test_key_works_with_a_version_1_file(capsys, fake_adb, folder) -> None:
    write(folder, _v1_text())
    code, out, err = invoke(capsys, fake_adb, "key", "KEYCODE_VOLUME_UP", "--json")
    assert (code, err) == (2, "")
    assert json.loads(out)["error"]["message"] == (
        f"KEYCODE_VOLUME_UP is refused by {SETTINGS_FILE}; remove it there to send it"
    )
    code, out, err = invoke(capsys, fake_adb, "key", "back")
    assert (code, out, err) == (0, "pressed key KEYCODE_BACK\n", "")


def test_a_version_1_file_refuses_warned_screens_and_start_over_words(
    tmp_path,
) -> None:
    policy = policy_from_settings(loaded(tmp_path, _v1_text()))
    found = run(_warned_screen(), policy=policy)
    assert reasons(found, "e3") == ("irreversible_screen", "confirm_button")
    assert reasons(found, "e4") == ("irreversible_screen",)
    for label in ("Restart", "Undo", "やり直す", "元に戻す"):
        item = el("e2", text=label, bounds=(50, 100, 250, 150))
        assert reasons(run([item], policy=policy), "e2") == ("deny_word",)


# -- exception words ([deny] except, none built in) ------------------------------


def _v2_text() -> str:
    path = Path(__file__).parent / "data" / "settings_v2.toml"
    text = path.read_text(encoding="utf-8")
    assert "defaults_version = 2\n" in text
    assert "except" not in text
    return text


def with_except(items: str, text: str | None = None) -> str:
    text = render_default_settings() if text is None else text
    if "except = []\n" in text:
        return text.replace("except = []\n", f"except = {items}\n")
    return text.replace("remove = []", f"except = {items}\nremove = []", 1)


AUTO_SCREEN = Screen(
    "auto",
    f"{SHOP}/.Auto",
    (
        El((40, 100, 280, 150), text="Automatically", go="auto"),
        El((40, 160, 280, 210), text="Border", go="auto"),
        El((40, 220, 280, 270), text="Automatically pay", go="auto"),
        El((40, 280, 280, 330), text="Bill my card automatically", go="auto"),
    ),
)
ALL_REFUSED = {
    "Automatically": ["deny_word"],
    "Border": ["deny_word"],
    "Automatically pay": ["deny_word"],
    "Bill my card automatically": ["deny_word"],
}


@pytest.mark.parametrize("version", [1, 2, 0])
def test_without_exceptions_the_crawl_decides_as_before(
    capsys, fake_adb, folder, version
) -> None:
    text = {1: _v1_text, 2: _v2_text, 0: render_default_settings}[version]()
    write(folder, text)
    FakeAndroid(fake_adb, SERIAL, (AUTO_SCREEN,), start="auto")
    payload = crawl_json(capsys, fake_adb)
    assert pressed(payload) == []
    assert skipped(payload) == ALL_REFUSED


@pytest.mark.parametrize("version", [1, 2, 0])
def test_an_exception_in_the_file_lets_its_word_through(
    capsys, fake_adb, folder, version
) -> None:
    text = {1: _v1_text, 2: _v2_text, 0: render_default_settings}[version]()
    write(folder, with_except('["automatically", "border"]', text))
    FakeAndroid(fake_adb, SERIAL, (AUTO_SCREEN,), start="auto")
    payload = crawl_json(capsys, fake_adb)
    # The risk the person who adds the word takes on: "Bill my card
    # automatically" has no deny word of its own and is pressed too.
    assert pressed(payload) == ["Automatically", "Border", "Bill my card automatically"]
    assert skipped(payload) == {"Automatically pay": ["deny_word"]}


def test_remove_takes_an_exception_out_again(capsys, fake_adb, folder) -> None:
    text = with_except('["automatically", "border"]', _v2_text())
    write(folder, text.replace("remove = []", 'remove = ["Automatically"]', 1))
    FakeAndroid(fake_adb, SERIAL, (AUTO_SCREEN,), start="auto")
    payload = crawl_json(capsys, fake_adb)
    assert pressed(payload) == ["Border"]


def test_a_dropped_builtin_exception_warns(
    capsys, fake_adb, folder, monkeypatch
) -> None:
    # Only a later version could add a built-in exception; simulate one.
    later = settings.DEFAULTS_VERSION + 1
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", later)
    monkeypatch.setattr(settings, "_DENY_EXCEPTIONS", ((later, "border"),))
    write(folder, _v2_text().replace("words = [\n", 'words = [\n    "border",\n', 1))
    FakeAndroid(fake_adb, SERIAL, (AUTO_SCREEN,), start="auto")
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json")
    assert code == 0
    assert err == (
        "uiprobe: warning: [deny] the default exception 'border' is not applied"
        " because it equals the deny word 'border'\n"
    )
    assert pressed(json.loads(out)) == []


def test_a_listed_exception_equal_to_a_deny_word_stops_the_crawl(
    capsys, fake_adb, folder
) -> None:
    write(folder, with_except('["border", "pay"]'))
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run")
    assert (code, out) == (2, "")
    assert "[deny] except" in err
    assert "remove the deny word instead" in err
    assert fake_adb.requests == []


def test_exceptions_come_from_the_file(tmp_path) -> None:
    policy = policy_from_settings(loaded(tmp_path, with_except('["border"]')))
    assert policy.deny_exceptions == ("border",)
    item = el("e2", text="Border", bounds=(50, 100, 250, 150))
    assert reasons(run([item]), "e2") == ("deny_word",)
    assert reasons(run([item], policy=policy), "e2") == ()


DRAMA_SCREEN = Screen(
    "drama",
    f"{SHOP}/.Drama",
    (
        El((40, 100, 280, 150), text="Automatically", go="drama"),
        El((40, 160, 280, 210), text="Dramatic", go="drama"),
        El((40, 220, 280, 270), text="Postal", go="drama"),
    ),
)


def test_a_word_added_in_the_file_refuses_without_exceptions(
    capsys, fake_adb, folder
) -> None:
    write(folder, edited('    "call",\n', '    "call",\n    "matic",\n'))
    FakeAndroid(fake_adb, SERIAL, (DRAMA_SCREEN,), start="drama")
    payload = crawl_json(capsys, fake_adb)
    assert skipped(payload) == {
        "Automatically": ["deny_word"],
        "Dramatic": ["deny_word"],
        "Postal": ["deny_word"],
    }


def test_an_exception_in_the_file_excuses_every_word(capsys, fake_adb, folder) -> None:
    # A word written under except excuses the built-in words ("post" in
    # "Postal") and the words the file or --deny adds ("matic" in
    # "Dramatic") alike.
    text = edited('    "call",\n', '    "call",\n    "matic",\n')
    write(folder, with_except('["dramatic", "postal"]', text))
    FakeAndroid(fake_adb, SERIAL, (DRAMA_SCREEN,), start="drama")
    payload = crawl_json(capsys, fake_adb, "--deny", "drama")
    assert pressed(payload) == ["Dramatic", "Postal"]
    assert skipped(payload) == {"Automatically": ["deny_word"]}


CONFIRM_SCREEN = Screen(
    "confirm",
    f"{SHOP}/.Confirm",
    (
        El((40, 100, 280, 150), text="Debit $50 automatically?"),
        El((40, 300, 280, 350), text="Continue", go="confirm"),
    ),
)


def test_an_exception_in_the_file_does_not_weaken_the_confirm_check(
    capsys, fake_adb, folder
) -> None:
    write(folder, with_except('["automatically"]'))
    FakeAndroid(fake_adb, SERIAL, (CONFIRM_SCREEN,), start="confirm")
    payload = crawl_json(capsys, fake_adb)
    assert pressed(payload) == []
    assert skipped(payload) == {"Continue": ["confirm_button"]}
