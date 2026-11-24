"""Tests for the navigate-up and slider rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tests.guards.helpers import BUTTON, PKG, decision, el, reasons, run
from uiprobe.guards import (
    REASONS,
    Judgement,
    Policy,
    judge,
)
from uiprobe.model import Bounds, Element, Screen

# -- the up button of a toolbar -------------------------------------------------------

NO_EDGES = Policy(avoid=())


IMAGE_BUTTON = "android.widget.ImageButton"


def toolbar(
    *,
    cls="android.view.ViewGroup",
    rid="com.android.settings:id/action_bar",
    up_desc="Navigate up",
    up_cls=IMAGE_BUTTON,
) -> list[Element]:
    """A Settings sub-page as recorded on the API 36 and 37 emulators: the
    dump reports the Toolbar as ``android.view.ViewGroup`` with the id
    ``:id/action_bar``; its first child is the clickable up button, then a
    title; below it two rows."""
    bar = el("e2", cls=cls, rid=rid, bounds=(0, 24, 320, 80), flags=())
    up = el("e3", parent="e2", cls=up_cls, desc=up_desc, bounds=(16, 24, 72, 80))
    title = el(
        "e4", parent="e2", cls="android.view.View", bounds=(72, 24, 320, 80), flags=()
    )
    wifi = el(
        "e5", text="Wi-Fi", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 170)
    )
    mobile = el(
        "e6", text="SIMs", cls="android.widget.LinearLayout", bounds=(0, 170, 320, 240)
    )
    return [bar, up, title, wifi, mobile]


def up_detail(judgement: Judgement, ident: str) -> tuple[str, str]:
    found = decision(judgement, ident)
    assert found.reasons == ("navigate_up",)
    assert [d.reason for d in found.details] == ["navigate_up"]
    return found.details[0].note, found.details[0].element_id


def test_the_recorded_up_button_is_refused_by_its_structure() -> None:
    judgement = run(toolbar(), policy=NO_EDGES)
    assert up_detail(judgement, "e3") == ("toolbar up button", "e3")


@pytest.mark.parametrize(
    ("cls", "rid"),
    [
        ("android.widget.Toolbar", ""),
        ("androidx.appcompat.widget.Toolbar", ""),
        ("com.google.android.material.appbar.MaterialToolbar", ""),
        ("com.example.ShopToolbar", ""),
        ("android.view.ViewGroup", "com.example.shop:id/action_bar"),
    ],
)
@pytest.mark.parametrize(
    "up_cls",
    [
        IMAGE_BUTTON,
        "androidx.appcompat.widget.AppCompatImageButton",
        "android.widget.ImageView",
    ],
)
def test_a_toolbar_up_button_with_another_label_is_refused(cls, rid, up_cls) -> None:
    # A device in German: the label does not say "Navigate up"; the structure does.
    elements = toolbar(cls=cls, rid=rid, up_desc="Nach oben", up_cls=up_cls)
    judgement = run(elements, policy=NO_EDGES)
    assert up_detail(judgement, "e3") == ("toolbar up button", "e3")


def test_only_the_first_clickable_child_of_a_toolbar_is_its_up_button() -> None:
    elements = toolbar(up_desc="Nach oben")
    # The title is not clickable and comes first; a search button comes last.
    elements[1], elements[2] = elements[2], elements[1]
    search = el(
        "e7", parent="e2", cls=IMAGE_BUTTON, desc="Search", bounds=(264, 24, 320, 80)
    )
    judgement = run([*elements, search], policy=NO_EDGES)
    assert up_detail(judgement, "e3")[0] == "toolbar up button"
    assert reasons(judgement, "e7") == ()


@pytest.mark.parametrize(
    ("cls", "rid", "up_cls"),
    [
        ("android.view.ViewGroup", "", IMAGE_BUTTON),
        ("android.view.ViewGroup", "com.example:id/action_bar_container", IMAGE_BUTTON),
        ("com.example.ToolbarLayout", "", IMAGE_BUTTON),
        ("android.widget.Toolbar", "", BUTTON),
    ],
)
def test_an_image_button_outside_a_toolbar_is_not_an_up_button(
    cls, rid, up_cls
) -> None:
    elements = toolbar(cls=cls, rid=rid, up_desc="Nach oben", up_cls=up_cls)
    assert reasons(run(elements, policy=NO_EDGES), "e3") == ()


@pytest.mark.parametrize("rid", ["android:id/home", "com.example.shop:id/up"])
def test_the_action_bar_up_ids_are_refused(rid) -> None:
    up = el(
        "e2",
        cls="android.widget.ImageView",
        rid=rid,
        desc="Nach oben",
        bounds=(16, 24, 72, 80),
    )
    judgement = run([up], policy=NO_EDGES)
    assert up_detail(judgement, "e2") == (f"resource id {rid}", "e2")


@pytest.mark.parametrize(
    "rid", ["android:id/homepage", "com.example:id/up_button", "home"]
)
def test_a_near_miss_up_id_is_not_refused(rid) -> None:
    up = el(
        "e2",
        cls="android.widget.ImageView",
        rid=rid,
        desc="Nach oben",
        bounds=(16, 24, 72, 80),
    )
    assert reasons(run([up], policy=NO_EDGES), "e2") == ()


def test_the_label_navigate_up_is_refused_anywhere() -> None:
    plain = el("e2", desc="Navigate up", bounds=(16, 24, 72, 80))
    judgement = run([plain], policy=NO_EDGES)
    assert up_detail(judgement, "e2") == ("label 'Navigate up'", "e2")


def test_the_compose_up_button_is_refused_by_its_label() -> None:
    # As recorded on a Settings page drawn with Compose: a clickable View
    # whose child View holds the content description.
    up = el("e2", cls="android.view.View", bounds=(4, 28, 52, 76))
    icon = el(
        "e3",
        parent="e2",
        cls="android.view.View",
        desc="Navigate up",
        bounds=(16, 40, 40, 64),
        flags=(),
    )
    shell = el("e4", parent="e2", cls=BUTTON, bounds=(8, 32, 48, 72), flags=())
    judgement = run([up, icon, shell], policy=NO_EDGES)
    assert up_detail(judgement, "e2") == ("label 'Navigate up'", "e2")


@pytest.mark.parametrize("desc", ["navigate up", "Navigate up to Settings", "Navigate"])
def test_only_the_exact_label_counts(desc) -> None:
    plain = el("e2", desc=desc, bounds=(16, 24, 72, 80))
    assert reasons(run([plain], policy=NO_EDGES), "e2") == ()


def test_the_rows_below_a_toolbar_are_not_refused() -> None:
    judgement = run(toolbar(), policy=NO_EDGES)
    assert reasons(judgement, "e5") == ()
    assert reasons(judgement, "e6") == ()


def test_the_up_rule_is_on_by_default_and_its_switch_restores_the_old_decisions() -> (
    None
):
    assert Policy().skip_navigate_up is True
    elements = toolbar()
    for policy in (NO_EDGES, Policy()):
        on = run(elements, policy=policy)
        off = run(elements, policy=replace(policy, skip_navigate_up=False))
        # Off: only the navigate_up reason and its detail are gone.
        for a, b in zip(on.decisions, off.decisions, strict=True):
            assert b.reasons == tuple(r for r in a.reasons if r != "navigate_up")
            assert b.details == tuple(d for d in a.details if d.reason != "navigate_up")
        assert (reasons(on, "e3"), reasons(off, "e3")) == (("navigate_up",), ())


def test_the_up_reason_comes_with_the_other_reasons_in_order() -> None:
    # The recorded API 36 button (0, 24, 56, 80) is at the left edge too.
    elements = toolbar()
    elements[1] = replace(elements[1], bounds=Bounds(0, 24, 56, 80))
    assert reasons(run(elements), "e3") == ("navigate_up", "screen_edge")


# -- sliders ---------------------------------------------------------------------------

SEEK_BAR = "android.widget.SeekBar"


PHONE = (1080, 2400)


def run_phone(elements, *, policy=None):
    """Judge `elements` on a 1080 x 2400 display, as on the Samsung phone."""
    top = Element(
        "e1", None, "android.widget.FrameLayout", Bounds(0, 0, *PHONE), package=PKG
    )
    screen = Screen(
        serial="R3CT00000000",
        captured_at=datetime(2026, 11, 17, tzinfo=UTC),
        package=PKG,
        activity=".Main",
        api_level=36,
        display=PHONE,
        elements=(top, *elements),
    )
    return judge(screen, PKG, policy or Policy(), foreground=PKG)


def vibration_row(*, bar_flags=("focusable",)) -> list[Element]:
    """The "Notification vibration" row of the Samsung phone's Settings, as
    reported: a clickable row holding an icon and a SeekBar with a content
    description; the row's press point (540, 2241) lies on the SeekBar."""
    row = el("e2", cls="android.widget.LinearLayout", bounds=(30, 2136, 1050, 2346))
    icon = el(
        "e3",
        parent="e2",
        cls="android.widget.ImageView",
        bounds=(54, 2199, 138, 2283),
        flags=(),
    )
    bar = el(
        "e4",
        parent="e2",
        cls=SEEK_BAR,
        desc="Notification vibration",
        bounds=(168, 2199, 1050, 2283),
        flags=bar_flags,
    )
    return [row, icon, bar]


def test_the_samsung_vibration_row_is_refused_for_its_slider() -> None:
    found = decision(run_phone(vibration_row()), "e2")
    assert found.point == (540, 2241)
    assert found.reasons == ("slider",)
    assert [(d.reason, d.note, d.element_id) for d in found.details] == [
        ("slider", "subtree", "e4")
    ]


def test_a_clickable_seek_bar_is_refused_itself() -> None:
    judgement = run_phone(vibration_row(bar_flags=("clickable", "focusable")))
    assert reasons(judgement, "e4") == ("slider",)
    assert decision(judgement, "e4").details[0].element_id == "e4"
    assert reasons(judgement, "e2") == ("slider",)


@pytest.mark.parametrize(
    "cls",
    [
        SEEK_BAR,
        "android.widget.AbsSeekBar",
        "androidx.appcompat.widget.AppCompatSeekBar",
        "android.widget.RatingBar",
        "androidx.appcompat.widget.AppCompatRatingBar",
        "com.google.android.material.slider.Slider",
        "com.google.android.material.slider.RangeSlider",
        "com.example.Widgets$VolumeSlider",
    ],
)
def test_every_adjustable_control_is_a_slider(cls) -> None:
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 200))
    control = el(
        "e3", parent="e2", cls=cls, desc="Level", bounds=(0, 300, 320, 340), flags=()
    )
    found = decision(run([row, control]), "e2")
    assert found.reasons == ("slider",)
    assert found.details[0].element_id == "e3"


@pytest.mark.parametrize(
    "cls",
    [
        "android.widget.ProgressBar",
        "com.example.SliderLayout",
        "com.example.SeekBarRow",
        "android.widget.TextView",
    ],
)
def test_other_classes_are_not_sliders(cls) -> None:
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 200))
    inner = el(
        "e3", parent="e2", cls=cls, desc="Level", bounds=(0, 120, 320, 180), flags=()
    )
    assert reasons(run([row, inner]), "e2") == ()


def test_a_press_point_on_a_slider_outside_the_subtree_is_refused() -> None:
    # The slider is drawn below the row, so the row's press point lies on it
    # although the row does not hold it; the press-point logic always takes
    # the centre, so it cannot step aside.
    bar = el("e2", cls=SEEK_BAR, desc="Volume", bounds=(0, 140, 320, 160), flags=())
    row = el("e3", text="Volume", bounds=(0, 100, 320, 200))
    other = el("e4", text="Ringtone", bounds=(0, 200, 320, 300))
    judgement = run([bar, row, other])
    found = decision(judgement, "e3")
    assert found.point == (160, 150)
    assert found.reasons == ("slider",)
    assert [(d.note, d.element_id) for d in found.details] == [("press point", "e2")]
    # A row next to the slider, whose press point is off it, is pressable.
    assert reasons(judgement, "e4") == ()


def test_a_row_without_a_slider_is_not_refused() -> None:
    row = el("e5", cls="android.widget.LinearLayout", bounds=(30, 1900, 1050, 2100))
    title = el(
        "e6", parent="e5", text="Sound mode", bounds=(168, 1940, 1050, 2000), flags=()
    )
    judgement = run_phone([*vibration_row(), row, title])
    assert reasons(judgement, "e5") == ()
    assert reasons(judgement, "e2") == ("slider",)


def test_the_slider_switch_restores_the_old_decisions() -> None:
    assert Policy().skip_sliders is True
    elements = vibration_row(bar_flags=("clickable", "focusable"))
    on = run_phone(elements)
    off = run_phone(elements, policy=Policy(skip_sliders=False))
    for a, b in zip(on.decisions, off.decisions, strict=True):
        assert b.reasons == tuple(r for r in a.reasons if r != "slider")
        assert b.details == tuple(d for d in a.details if d.reason != "slider")
    assert (reasons(off, "e2"), reasons(off, "e4")) == ((), ())


def test_the_slider_reason_follows_navigate_up() -> None:
    assert REASONS[REASONS.index("navigate_up") + 1] == "slider"
    assert REASONS[REASONS.index("slider") + 1] == "deny_word"
    elements = vibration_row()
    elements[2] = replace(elements[2], content_desc="Delete")
    assert reasons(run_phone(elements), "e2") == ("slider", "deny_word")
