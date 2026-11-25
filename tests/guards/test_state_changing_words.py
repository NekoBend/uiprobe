"""Deny words for state-changing verbs of Android Settings (defaults version 7)."""

from __future__ import annotations

import pytest

from tests.guards.helpers import decision, el, reasons, run

SETTINGS = "com.android.settings"
LAYOUT = "android.widget.LinearLayout"
TEXT = "android.widget.TextView"


def _button_bar(*labels: str) -> list:
    """A saved-network page's button bar: clickable id-only LinearLayouts, each
    with a TextView child holding the label (ids action1, action2, ...)."""
    elements = [
        el("bar", cls=LAYOUT, flags=(), bounds=(0, 100, 320, 160), package=SETTINGS)
    ]
    for index, label in enumerate(labels, start=1):
        left = (index - 1) * 100
        elements.append(
            el(
                f"a{index}",
                parent="bar",
                cls=LAYOUT,
                rid=f"com.android.settings:id/action{index}",
                bounds=(left, 100, left + 100, 160),
                package=SETTINGS,
            )
        )
        elements.append(
            el(
                f"t{index}",
                parent=f"a{index}",
                text=label,
                cls=TEXT,
                flags=(),
                bounds=(left + 10, 110, left + 90, 150),
                package=SETTINGS,
            )
        )
    return elements


def _run(elements: list):
    return run(
        elements,
        foreground=SETTINGS,
        target=SETTINGS,
        package=SETTINGS,
        root_package=SETTINGS,
    )


def test_forget_in_a_saved_network_button_bar_is_refused() -> None:
    judgement = _run(_button_bar("Forget", "Disconnect", "Share"))
    assert reasons(judgement, "a1") == ("deny_word",)
    detail = decision(judgement, "a1").details[0]
    assert (detail.word, detail.field, detail.element_id) == ("forget", "text", "t1")


def test_disconnect_in_the_same_bar_is_refused() -> None:
    judgement = _run(_button_bar("Forget", "Disconnect", "Share"))
    assert reasons(judgement, "a2") == ("deny_word",)
    assert decision(judgement, "a2").details[0].word == "disconnect"
    assert reasons(judgement, "a3") == ("deny_word",)


@pytest.mark.parametrize(
    "label",
    [
        "Network & internet",
        "Connected devices",
        "Apps",
        "Internet",
        "Pair new device",
        "Device name",
        "Bluetooth",
        "Display",
        "Notifications",
        "Battery",
        "Storage",
        "ネットワークとインターネット",
        "接続済みのデバイス",
        "アプリ",
        "デバイス名",
    ],
)
def test_ordinary_settings_rows_stay_pressable(label: str) -> None:
    elements = [
        el("row", cls=LAYOUT, bounds=(0, 100, 320, 160), package=SETTINGS),
        el(
            "title",
            parent="row",
            text=label,
            cls=TEXT,
            flags=(),
            bounds=(10, 110, 300, 150),
            package=SETTINGS,
        ),
    ]
    assert reasons(_run(elements), "row") == ()


@pytest.mark.parametrize(
    "label",
    [
        "Forget",
        "Forget network",
        "Disconnect",
        "Unpair",
        "Turn off",
        "Turn off Wi-Fi",
        "Revoke access",
        "Eject",
        "Unmount",
        "Unlink",
        "Deactivate this device admin app",
        "切断",
        "接続を解除",
        "ペアリングを解除",
        "Wi-Fiをオフにする",
        "取り出す",
        "マウント解除",
        "リンク解除",
    ],
)
def test_state_changing_labels_are_refused(label: str) -> None:
    judgement = _run([el("b", text=label, package=SETTINGS)])
    assert reasons(judgement, "b") == ("deny_word",), label


def test_separate_buttons_in_the_same_bar_stay_pressable() -> None:
    judgement = _run(_button_bar("Forget", "Edit", "Cancel"))
    assert reasons(judgement, "a1") == ("deny_word",)
    assert reasons(judgement, "a2") == ()
    assert reasons(judgement, "a3") == ()


@pytest.mark.parametrize(
    "label",
    ["Unpaired devices", "Disconnection history", "Revoked certificates", "切断履歴"],
)
def test_the_accepted_over_blocks_are_refused(label: str) -> None:
    assert reasons(_run([el("b", text=label, package=SETTINGS)]), "b") == ("deny_word",)
