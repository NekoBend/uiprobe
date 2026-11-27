"""Tests for ``[rules] also_target``: the settings key, the refusals before
anything is sent, and the read of the device's home app."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from uiprobe import settings
from uiprobe.also_target import (
    FIXED_HOME_APPS,
    HOME_COMMAND,
    HOME_UNREADABLE,
    SYSTEM_UI,
    AlsoTargetError,
    check_also_target,
    check_home,
    not_installed_error,
)
from uiprobe.device import _home_package
from uiprobe.guards import Policy
from uiprobe.settings import SETTINGS_FILE, SettingsError, load_settings

DATA = Path(__file__).parent / "data"
HELPER = "com.example.helper"
SHOP = "com.example.shop"
ENTRY = (
    '# also_target = ["com.example.helper"]   # apps the crawl may also press in;'
    " system UI and the home app are refused, the controls of permission screens"
    " are never tapped (they get BACK when back_on_other_screens is true)"
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / SETTINGS_FILE
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def with_also(text: str, value: str) -> str:
    """The default file with ``also_target = value`` under [rules]."""
    return text.replace("[rules]\n", f"[rules]\nalso_target = {value}\n", 1)


# -- the settings key ----------------------------------------------------------


def test_the_generated_file_is_current_and_writes_the_key_commented():
    text = settings.render_default_settings()
    assert "format_version = 1\ndefaults_version = 12\n" in text
    assert settings.DEFAULTS_VERSION == 12
    assert text.count("also_target") == 1
    assert ENTRY + "\n" in text
    rules = text.split("[rules]\n")[1].split("[limits]")[0]
    assert ENTRY in rules
    assert "also_target" not in tomllib.loads(text)["rules"]


def test_the_generated_file_loads_with_an_empty_list(tmp_path):
    loaded = load_settings(write(tmp_path, settings.render_default_settings()))
    assert loaded.also_target == ()
    assert settings.policy_from_settings(loaded).also_target == ()


@pytest.mark.parametrize("version", range(1, 13))
def test_every_defaults_version_loads_with_an_empty_list(tmp_path, version):
    if version <= 11:
        text = (DATA / f"settings_v{version}.toml").read_text(encoding="utf-8")
    else:
        text = settings.render_default_settings()
        text = text.replace(ENTRY + "\n", "")
        assert "also_target" not in text
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == version
    assert loaded.also_target == ()
    assert loaded.warnings == ()


@pytest.mark.parametrize("version", range(1, 12))
def test_every_defaults_version_may_list_packages(tmp_path, version):
    if version <= 10:
        text = (DATA / f"settings_v{version}.toml").read_text(encoding="utf-8")
    else:
        text = settings.render_default_settings()
    path = write(tmp_path, with_also(text, f'["{HELPER}", "com.example.two"]'))
    loaded = load_settings(path)
    assert loaded.also_target == (HELPER, "com.example.two")
    assert settings.policy_from_settings(loaded).also_target == loaded.also_target


def test_a_current_file_may_leave_the_key_out(tmp_path):
    text = settings.render_default_settings()
    assert "also_target" in text  # only as the comment
    assert load_settings(write(tmp_path, text)).also_target == ()


@pytest.mark.parametrize(
    "value",
    [
        '["notapackage"]',
        '["com.example.1bad"]',
        '["com..example"]',
        '[""]',
        '["com.example.a b"]',
        "[1]",
        '"com.example.helper"',
        "true",
        '["com.example.x", "com.example.x"]',
    ],
)
def test_an_entry_is_checked_like_the_commands_package(tmp_path, value):
    path = write(tmp_path, with_also(settings.render_default_settings(), value))
    with pytest.raises(SettingsError) as info:
        load_settings(path)
    assert "[rules] also_target" in str(info.value)
    assert str(path) in str(info.value)


def test_a_duplicate_entry_is_named(tmp_path):
    value = '["com.example.x", "com.example.x"]'
    path = write(tmp_path, with_also(settings.render_default_settings(), value))
    with pytest.raises(SettingsError, match="duplicate entry"):
        load_settings(path)


def test_the_settings_fields_end_with_also_target():
    import dataclasses

    names = [f.name for f in dataclasses.fields(settings.Settings)]
    # later fields follow in the order of the defaults versions
    assert names[-7:-1] == [
        "also_target",
        "back_on_other_screens",
        "scroll_lists",
        "scrolls_per_screen",
        "scroll_amount",
        "scroll_incidents",
    ]
    assert names[-1] == "legacy_confirm_words"
    assert names[-8] == "record_text"


def test_no_default_exception_is_tagged_with_the_new_version():
    assert settings._FIRST_EXCEPT == settings.DEFAULTS_VERSION + 1


# -- the checks that need no device ---------------------------------------------


def test_a_good_list_is_returned_in_order():
    assert check_also_target(SHOP, [HELPER, "com.example.two"]) == (
        HELPER,
        "com.example.two",
    )
    assert check_also_target(SHOP, []) == ()


def test_the_system_ui_is_refused_with_its_reason():
    with pytest.raises(AlsoTargetError) as info:
        check_also_target(SHOP, [HELPER, SYSTEM_UI])
    assert SYSTEM_UI == "com.android.systemui"
    assert str(info.value) == (
        "also_target cannot list com.android.systemui: it is the system UI (lock"
        " screen, notification shade); nothing was pressed"
    )
    assert isinstance(info.value, ValueError)


@pytest.mark.parametrize(
    "launcher",
    [
        "com.android.launcher3",
        "com.google.android.apps.nexuslauncher",
        "com.sec.android.app.launcher",
    ],
)
def test_the_fixed_home_apps_are_refused(launcher):
    assert launcher in FIXED_HOME_APPS
    with pytest.raises(AlsoTargetError) as info:
        check_also_target(SHOP, [launcher])
    assert str(info.value) == (
        f"also_target cannot list {launcher}: it is a home app (launcher);"
        " nothing was pressed"
    )


def test_the_fixed_list_is_exactly_these_three():
    assert FIXED_HOME_APPS == (
        "com.android.launcher3",
        "com.google.android.apps.nexuslauncher",
        "com.sec.android.app.launcher",
    )


def test_the_target_itself_is_refused():
    with pytest.raises(AlsoTargetError) as info:
        check_also_target(SHOP, [SHOP])
    assert str(info.value) == (
        f"also_target lists {SHOP}, which is the target app itself;"
        " remove it from the list"
    )


def test_a_duplicate_is_refused():
    with pytest.raises(AlsoTargetError, match="more than once"):
        check_also_target(SHOP, [HELPER, HELPER])


@pytest.mark.parametrize("bad", ["x", "com.1x", "com.example.", 5, None])
def test_a_bad_name_is_refused(bad):
    with pytest.raises(AlsoTargetError, match="also_target: "):
        check_also_target(SHOP, [bad])


def test_one_string_is_not_a_list():
    with pytest.raises(AlsoTargetError, match="list of package names"):
        check_also_target(SHOP, HELPER)


def test_a_listed_home_app_of_the_device_is_refused():
    with pytest.raises(AlsoTargetError) as info:
        check_home([HELPER, "com.vendor.home"], "com.vendor.home")
    assert str(info.value) == (
        "also_target cannot list com.vendor.home: it is the home app of this"
        " device; nothing was pressed"
    )


def test_another_home_app_passes():
    assert check_home([HELPER], "com.vendor.home") is None


def test_an_unreadable_home_app_refuses_the_run():
    with pytest.raises(AlsoTargetError) as info:
        check_home([HELPER], None)
    assert str(info.value) == (
        "also_target: could not read this device's home app, which also_target"
        " needs in order to refuse it; nothing was pressed"
    )
    assert str(info.value) == HOME_UNREADABLE


def test_the_not_installed_refusal_names_the_package():
    error = not_installed_error(HELPER)
    assert str(error) == (
        f"also_target lists {HELPER}, which is not installed on this device"
        " (or its uid could not be read); nothing was pressed"
    )


# -- the home read ----------------------------------------------------------------

INFO = "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true"


def test_the_home_command_is_the_read_only_resolve():
    assert HOME_COMMAND == (
        "cmd package resolve-activity --brief -a android.intent.action.MAIN"
        " -c android.intent.category.HOME"
    )


@pytest.mark.parametrize(
    ("output", "package"),
    [
        (
            f"{INFO}\ncom.android.launcher3/.Launcher\nuiprobe-exit:0\n",
            "com.android.launcher3",
        ),
        ("com.vendor.home/.Home\nuiprobe-exit:0\n", "com.vendor.home"),
        (
            "com.vendor.home/com.vendor.home.ui.Main\r\nuiprobe-exit:0\r\n",
            "com.vendor.home",
        ),
        ("\ncom.vendor.home/.Home\n\nuiprobe-exit:0\n\n", "com.vendor.home"),
    ],
)
def test_the_package_comes_from_the_one_component_line(output, package):
    assert _home_package(output) == package


@pytest.mark.parametrize(
    "output",
    [
        "",
        "uiprobe-exit:0\n",
        "No activity found\nuiprobe-exit:0\n",
        "com.vendor.home/.Home\n",  # no marker: output may be cut
        "com.vendor.home/.Home\nuiprobe-exit:1\n",
        "com.vendor.home/.Home\nuiprobe-exit:0\nextra\n",
        # a line that is not an information line, or a second component
        "some other line\ncom.vendor.home/.Home\nuiprobe-exit:0\n",
        "com.vendor.home/.Home\ncom.other.home/.Home\nuiprobe-exit:0\n",
        "error: closed\n",
        "com.vendor.home\nuiprobe-exit:0\n",  # no component
        "android/com.android.internal.app.ResolverActivity\nuiprobe-exit:0\n",
        "com.vendor.home/bad name\nuiprobe-exit:0\n",
        "1bad.pkg/.Home\nuiprobe-exit:0\n",
    ],
)
def test_output_that_names_no_home_app_gives_none(output):
    assert _home_package(output) is None


def test_a_direct_preview_refuses_the_names_that_need_no_device():
    from uiprobe import preview

    class NoDevice:
        def dump(self):
            raise AssertionError("the device must not be read")

    for listed in ((SYSTEM_UI,), (FIXED_HOME_APPS[0],), (SHOP,), (HELPER, HELPER)):
        with pytest.raises(AlsoTargetError):
            preview(NoDevice(), SHOP, Policy(also_target=listed))  # ty: ignore[invalid-argument-type]


# -- every entry point refuses ----------------------------------------------------


@pytest.mark.parametrize(
    "listed",
    [
        (SYSTEM_UI,),
        (HELPER, SYSTEM_UI),
        *[(name,) for name in FIXED_HOME_APPS],
        ("nope",),
        ("com." + "a" * 252,),  # 256 characters
        (HELPER, HELPER),
    ],
)
def test_a_policy_cannot_be_built_with_a_refused_entry(listed):
    with pytest.raises(AlsoTargetError):
        Policy(also_target=listed)


def test_a_policy_keeps_a_good_list_and_replace_checks_again():
    import dataclasses

    good = Policy(also_target=(HELPER,))
    assert good.also_target == (HELPER,)
    with pytest.raises(AlsoTargetError):
        dataclasses.replace(good, also_target=(SYSTEM_UI,))


def test_an_engine_cannot_be_built_for_a_policy_that_lists_its_target():
    from uiprobe.crawl import CrawlEngine, Limits

    CrawlEngine(SHOP, Policy(also_target=(HELPER,)), Limits(), started_at=0.0)
    with pytest.raises(AlsoTargetError, match="target app itself"):
        CrawlEngine(SHOP, Policy(also_target=(SHOP,)), Limits(), started_at=0.0)


class _Home:
    """A device that only knows its home app, and fails on any screen read."""

    def __init__(self, home: str | None) -> None:
        self.home = home
        self.reads = 0

    def home_package(self) -> str | None:
        self.reads += 1
        return self.home

    def dump(self):
        raise AssertionError("no screen may be read")


def test_a_direct_preview_reads_the_home_app_and_fails_closed():
    from uiprobe import preview

    policy = Policy(also_target=(HELPER,))
    unreadable = _Home(None)
    with pytest.raises(AlsoTargetError) as info:
        preview(unreadable, SHOP, policy)  # ty: ignore[invalid-argument-type]
    assert str(info.value) == HOME_UNREADABLE
    assert unreadable.reads == 1
    listed = _Home(HELPER)
    with pytest.raises(AlsoTargetError, match="home app of this device"):
        preview(listed, SHOP, policy)  # ty: ignore[invalid-argument-type]
    assert listed.reads == 1


def test_a_direct_preview_without_a_list_reads_no_home_app():
    from uiprobe import preview

    device = _Home("com.vendor.home")
    with pytest.raises(AssertionError, match="no screen"):
        preview(device, SHOP, Policy())  # ty: ignore[invalid-argument-type]
    assert device.reads == 0
