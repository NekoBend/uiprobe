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


def test_a_back_is_not_sent_when_the_app_uid_changed_since_the_start(
    tmp_path, device, android, clock
):
    driver, _, observation = _gate_run(tmp_path, device, clock)
    assert driver.app_uid is not None
    driver.app_uid = PackageUid(driver.app_uid.uid + 1, driver.app_uid.shared)
    keys = _keys(android)
    driver.send(Back(), observation)
    assert _keys(android) == keys
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert _end(tmp_path)["details"] == {"unreadable": "target_uid"}


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
