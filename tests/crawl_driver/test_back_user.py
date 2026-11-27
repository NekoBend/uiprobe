"""A BACK decided on a screen of the app goes only to a window of the app in
Android user 0, as a tap does (the same user and uid checks)."""

from __future__ import annotations

import pytest

from tests.crawl_driver.helpers import (
    BACK_POLICY,
    OTHER,
    SHOP,
    _gate_run,
    _not_sent_reason,
    lines,
)
from uiprobe.crawl import Back
from uiprobe.errors import AdbError
from uiprobe.guards import Policy
from uiprobe.packageuid import PackageUid


def _keys(android) -> int:
    return len([i for i in android.inputs if i.kind == "key"])


def _end(tmp_path) -> dict[str, object]:
    return lines(tmp_path / "run" / "transitions.jsonl")[-1]


def _back(tmp_path, device, clock, monkeypatch, *, keyboard, policy=None):
    # A keyboard BACK is one decided with the keyboard up; the other is the
    # navigation BACK. The keyboard read is the same before and at the gate.
    if keyboard:
        monkeypatch.setattr(device, "keyboard_shown", lambda: True)
    driver, _, observation = _gate_run(tmp_path, device, clock, policy=policy)
    assert observation.keyboard_shown is keyboard
    return driver, observation


@pytest.mark.parametrize("keyboard", [False, True], ids=["navigation", "keyboard"])
def test_a_back_goes_to_the_apps_window_of_user_0(
    tmp_path, device, android, clock, monkeypatch, keyboard
):
    driver, observation = _back(tmp_path, device, clock, monkeypatch, keyboard=keyboard)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys + 1
    assert _not_sent_reason(tmp_path) is None


@pytest.mark.parametrize("keyboard", [False, True], ids=["navigation", "keyboard"])
def test_a_back_is_not_sent_to_the_same_package_of_user_10(
    tmp_path, device, android, clock, monkeypatch, keyboard
):
    # The work profile runs the same package as user 10.
    android.focus_user = 10
    driver, observation = _back(tmp_path, device, clock, monkeypatch, keyboard=keyboard)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_user"}


@pytest.mark.parametrize("keyboard", [False, True], ids=["navigation", "keyboard"])
def test_a_back_is_not_sent_to_a_window_of_the_uid_of_another_user(
    tmp_path, device, android, clock, monkeypatch, keyboard
):
    # The record says u0 but the owner uid is user 10's: user is not known.
    android.focus_owner_uid = 1010150
    driver, observation = _back(tmp_path, device, clock, monkeypatch, keyboard=keyboard)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_user"}


@pytest.mark.parametrize("keyboard", [False, True], ids=["navigation", "keyboard"])
def test_a_back_is_not_sent_to_a_window_owned_by_another_uid(
    tmp_path, device, android, clock, monkeypatch, keyboard
):
    android.focus_owner_uid = android.uid_of(SHOP) + 1
    driver, observation = _back(tmp_path, device, clock, monkeypatch, keyboard=keyboard)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_uid"}


def test_a_back_goes_out_when_the_owner_uid_is_not_read_for_user_0(
    tmp_path, device, android, clock, monkeypatch
):
    # As for a tap: an owner uid that was not read is not a mismatch.
    driver, _, _ = _gate_run(tmp_path, device, clock)
    real = device.focused_window()
    assert real is not None
    assert real.owner_uid is not None
    monkeypatch.setattr(device, "focused_window", lambda: real._replace(owner_uid=None))
    observation = driver.observe()
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys + 1
    assert _not_sent_reason(tmp_path) is None


def test_a_back_is_not_sent_when_the_app_uid_was_not_read_at_the_start(
    tmp_path, device, android, clock
):
    # As for a tap (every tap is refused then): fail closed.
    driver, _, observation = _gate_run(tmp_path, device, clock)
    driver.app_uid = None
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_uid"}


def test_a_back_is_not_sent_when_the_app_uid_read_now_differs(
    tmp_path, device, android, clock, monkeypatch
):
    # The cached uid and the focused window's uid are equal; only the fresh
    # reply of the package dump differs (the app was installed again).
    driver, _, observation = _gate_run(tmp_path, device, clock)
    assert driver.app_uid is not None
    changed = PackageUid(driver.app_uid.uid + 1, driver.app_uid.shared)
    monkeypatch.setattr(device, "package_uid", lambda _package: changed)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_uid"}


def test_the_uid_is_read_before_the_focused_window(
    tmp_path, device, clock, monkeypatch
):
    driver, _, observation = _gate_run(tmp_path, device, clock)
    order: list[str] = []
    for name in ("package_uid", "foreground", "focused_window", "keyboard_shown"):
        real = getattr(device, name)

        def spy(*args, _real=real, _name=name):
            order.append(_name)
            return _real(*args)

        monkeypatch.setattr(device, name, spy)
    driver.send(Back(), observation)
    assert order == ["package_uid", "foreground", "keyboard_shown", "focused_window"]


@pytest.mark.parametrize("keyboard", [False, True], ids=["navigation", "keyboard"])
def test_a_focus_that_moves_to_user_10_during_the_keyboard_read_is_not_sent_to(
    tmp_path, device, android, clock, monkeypatch, keyboard
):
    if keyboard:
        monkeypatch.setattr(device, "keyboard_shown", lambda: True)
    driver, _, observation = _gate_run(tmp_path, device, clock)
    real = device.keyboard_shown

    def moving():
        android.focus_user = 10
        return real()

    monkeypatch.setattr(device, "keyboard_shown", moving)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) in {"focus_other", "changed_before_send"}


def test_a_focus_that_moves_to_user_10_during_the_uid_read_is_not_sent_to(
    tmp_path, device, android, clock, monkeypatch
):
    driver, _, observation = _gate_run(tmp_path, device, clock)
    real = device.package_uid

    def moving(package):
        android.focus_user = 10
        return real(package)

    monkeypatch.setattr(device, "package_uid", moving)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) is not None


def test_a_uid_read_that_fails_closes_the_transition_as_not_sent(
    tmp_path, device, android, clock, monkeypatch
):
    # The tap path lets such an error end the run with a start line alone;
    # a BACK records it as not sent and the run goes on.
    driver, _, observation = _gate_run(tmp_path, device, clock)

    def fail(_package):
        msg = "timed out"
        raise AdbError(msg)

    monkeypatch.setattr(device, "package_uid", fail)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    end = _end(tmp_path)
    assert end["not_sent"] is True
    assert end["reason"] == "focus_other"
    assert end["details"] == {"unreadable": "target_uid"}


def test_an_unknown_keyboard_still_wins_over_the_uid_check(
    tmp_path, device, android, clock, monkeypatch
):
    driver, _, observation = _gate_run(tmp_path, device, clock)

    def fail(_package):
        msg = "timed out"
        raise AdbError(msg)

    monkeypatch.setattr(device, "package_uid", fail)
    monkeypatch.setattr(device, "keyboard_shown", lambda: None)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "keyboard_unknown"
    assert "details" not in _end(tmp_path)


def test_without_the_tap_window_check_the_uid_is_not_compared(
    tmp_path, device, android, clock
):
    # The tap path reads and compares no uid then.
    android.focus_owner_uid = android.uid_of(SHOP) + 1  # user 0, other uid
    policy = Policy(check_tap_window=False)
    driver, _, observation = _gate_run(tmp_path, device, clock, policy=policy)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys + 1
    assert _not_sent_reason(tmp_path) is None


def test_without_the_tap_window_check_the_user_is_still_checked(
    tmp_path, device, android, clock
):
    # The user comes from the focused window, which the gate reads anyway.
    android.focus_user = 10
    policy = Policy(check_tap_window=False)
    driver, _, observation = _gate_run(tmp_path, device, clock, policy=policy)
    driver.app_uid = None  # never read with the check off
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_user"}


def test_a_back_on_another_apps_screen_still_goes_to_a_window_of_user_10(
    tmp_path, device, android, clock
):
    # --back-on-other-screens: unchanged, the BACK goes to whatever has focus.
    driver, _, _ = _gate_run(tmp_path, device, clock, policy=BACK_POLICY)
    android.show("ext1")
    android.focus_user = 10
    observation = driver.observe()
    assert observation.foreground == OTHER
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys + 1
    assert _not_sent_reason(tmp_path) is None
