"""`Policy.also_target` in `judge`: a listed package's screens are judged
exactly like the target's, and nothing else is let through."""

from __future__ import annotations

import pytest

from tests.guards.helpers import (
    CLICK,
    PKG,
    el,
    make_screen,
    root,
)
from uiprobe.guards import PERMISSION_PACKAGES, Policy, judge

P = "com.example.helper"
LISTED = Policy(also_target=(P,))


def judged(
    elements,
    *,
    package=PKG,
    foreground=PKG,
    policy=None,
    target=PKG,
    root_bounds=(0, 0, 320, 640),
):
    """Judge `elements` (given for `package`) and return id -> (reasons, point)."""
    top = root(root_bounds, package=package)
    screen = make_screen([top, *elements], package=package)
    found = judge(screen, target, policy or Policy(), foreground=foreground)
    return {d.element.id: (tuple(d.reasons), d.point) for d in found.decisions}


# Every scene is built for a package; the same scene is judged as the target
# app's screen and as the screen of a listed package, and the answers must be
# the same, id for id. The second column is a reason the scene must give (None:
# the scene is pressable), so a guard that stopped working on a listed
# package's screen fails here.
def _scenes():
    def deny(pkg):
        return [el("e2", text="Delete account", package=pkg)]

    def plain(pkg):
        return [el("e2", text="Open", package=pkg)]

    def confirm(pkg):
        return [
            el("e2", text="OK", package=pkg),
            el(
                "e3",
                text="Delete account permanently?",
                flags=(),
                cls="android.widget.TextView",
                bounds=(50, 300, 250, 350),
                package=pkg,
            ),
        ]

    def irreversible(pkg):
        return [
            el("e2", text="Open", package=pkg),
            el(
                "e3",
                text="This action cannot be undone",
                flags=(),
                cls="android.widget.TextView",
                bounds=(50, 300, 250, 350),
                package=pkg,
            ),
        ]

    def toggle(pkg):
        return [el("e2", text="Wi-Fi", flags=(*CLICK, "checkable"), package=pkg)]

    def slider(pkg):
        return [el("e2", cls="android.widget.SeekBar", text="Volume", package=pkg)]

    def text_field(pkg):
        return [el("e2", cls="android.widget.EditText", text="name", package=pkg)]

    def password(pkg):
        return [
            el(
                "e2",
                cls="android.widget.EditText",
                flags=(*CLICK, "password"),
                package=pkg,
            )
        ]

    def edge(pkg):
        return [el("e2", text="Open", bounds=(50, 0, 250, 40), package=pkg)]

    def covered(pkg):
        return [
            el("e2", text="Open", package=pkg),
            el(
                "e3",
                text="Cover",
                flags=(),
                cls="android.widget.TextView",
                parent="e1",
                package=pkg,
            ),
        ]

    def hidden(pkg):
        return [el("e2", text="Open", bounds=(400, 100, 500, 150), package=pkg)]

    def two_target(pkg):
        return [
            el("e2", cls="android.widget.LinearLayout", text="Network", package=pkg),
            el(
                "e3",
                parent="e2",
                rid=f"{pkg}:id/two_target_divider",
                flags=(),
                cls="android.view.View",
                bounds=(60, 100, 62, 150),
                package=pkg,
            ),
        ]

    def unlabeled(pkg):
        return [el("e2", package=pkg)]

    def number(pkg):
        return [el("e2", text="42", package=pkg)]

    return [
        ("plain", plain, None),
        ("deny_word", deny, "deny_word"),
        ("confirm_button", confirm, "confirm_button"),
        ("irreversible_screen", irreversible, "irreversible_screen"),
        ("toggle", toggle, "toggle"),
        ("slider", slider, "slider"),
        ("text_input", text_field, "text_input"),
        ("password_field", password, "password_field"),
        ("screen_edge", edge, "screen_edge"),
        ("covered", covered, "covered"),
        ("not_visible", hidden, "not_visible"),
        ("two_target_row", two_target, "two_target_row"),
        ("unlabeled", unlabeled, "unlabeled"),
        ("number_only", number, "number_only"),
    ]


SCENES = _scenes()


@pytest.mark.parametrize(
    ("name", "scene", "reason"), SCENES, ids=[s[0] for s in SCENES]
)
def test_a_listed_packages_screen_is_judged_like_the_targets(name, scene, reason):
    del name
    main = judged(scene(PKG))
    listed = judged(scene(P), package=P, foreground=P, policy=LISTED)
    assert listed == main
    refusals = {r for reasons, _ in listed.values() for r in reasons}
    if reason is None:
        assert listed["e2"][0] == ()
        assert listed["e2"][1] is not None
    else:
        assert reason in refusals


def test_an_unlisted_package_is_refused_whatever_the_scene():
    for _, scene, _ in SCENES:
        found = judged(scene(P), package=P, foreground=P)
        assert all("outside_target_app" in r for r, _ in found.values())


def test_the_list_is_no_widening_for_the_main_app():
    # With a list, the main app's own screens are judged as before.
    for _, scene, _ in SCENES:
        assert judged(scene(PKG), policy=LISTED) == judged(scene(PKG))


def test_an_empty_list_changes_nothing():
    empty = Policy(also_target=())
    assert empty == Policy()
    for _, scene, _ in SCENES:
        assert judged(scene(PKG), policy=empty) == judged(scene(PKG))
        assert judged(scene(P), package=P, foreground=P, policy=empty) == judged(
            scene(P), package=P, foreground=P
        )


@pytest.mark.parametrize("permission", sorted(PERMISSION_PACKAGES))
def test_permission_screens_stay_refused_even_when_listed(permission):
    listed = Policy(also_target=(permission,))
    elements = [
        el(
            "e2",
            text="While using the app",
            rid=f"{permission}:id/permission_allow_foreground_only_button",
            package=permission,
        )
    ]
    found = judged(elements, package=permission, foreground=permission, policy=listed)
    reasons, point = found["e2"]
    assert "permission_screen" in reasons
    assert "outside_target_app" not in reasons  # it counts as the target
    assert point is not None


def test_a_permission_element_on_a_listed_screen_refuses_what_it_covers():
    elements = [
        el("e2", text="Open", package=P),
        el(
            "e3",
            text="Allow",
            parent="e1",
            bounds=(0, 0, 320, 640),
            package=next(iter(PERMISSION_PACKAGES)),
        ),
    ]
    found = judged(elements, package=P, foreground=P, policy=LISTED)
    assert "permission_screen" in found["e2"][0]


def test_an_element_of_the_main_app_on_a_listed_screen_is_outside():
    elements = [
        el("e2", text="Open", package=P),
        el("e3", text="Other", bounds=(50, 300, 250, 350), package=PKG),
    ]
    found = judged(elements, package=P, foreground=P, policy=LISTED)
    assert found["e2"][0] == ()
    assert "outside_target_app" in found["e3"][0]


def test_an_element_of_a_listed_package_on_a_main_screen_is_outside():
    elements = [
        el("e2", text="Open", package=PKG),
        el("e3", text="Float", bounds=(50, 300, 250, 350), package=P),
    ]
    found = judged(elements, policy=LISTED)
    assert found["e2"][0] == ()
    assert "outside_target_app" in found["e3"][0]
    # a listed package's element covering a main app's button refuses it too
    covering = [
        el("e2", text="Open", package=PKG),
        el(
            "e3",
            text="Float",
            flags=(),
            parent="e1",
            package=P,
            bounds=(0, 0, 320, 640),
        ),
    ]
    found = judged(covering, policy=LISTED)
    assert "outside_target_app" in found["e2"][0]


@pytest.mark.parametrize(
    ("package", "foreground", "note"),
    [
        (P, PKG, "screen package is"),  # a listed screen, the main app in front
        (PKG, P, "screen package is"),  # a main screen, a listed app in front
        (P, "com.example.third", "foreground is"),
        (P, None, "foreground app unknown"),
        ("com.example.third", "com.example.third", "foreground is"),
    ],
)
def test_the_screen_and_the_foreground_must_both_name_one_package(
    package, foreground, note
):
    elements = [el("e2", text="Open", package=package)]
    top = root(package=package)
    screen = make_screen([top, *elements], package=package)
    found = judge(screen, PKG, LISTED, foreground=foreground)
    (decision,) = found.decisions
    assert "outside_target_app" in decision.reasons
    assert note in decision.details[0].note


def test_the_target_itself_in_the_list_is_harmless_to_judge():
    # The command line refuses it; judge treats it as the target either way.
    same = Policy(also_target=(PKG,))
    scene = SCENES[0][1]
    assert judged(scene(PKG), policy=same) == judged(scene(PKG))


def test_the_field_is_a_tuple_of_strings():
    for bad in ("com.example.helper", (1,), None, 5):
        with pytest.raises(TypeError, match="also_target"):
            Policy(also_target=bad)  # ty: ignore[invalid-argument-type]


def test_with_deny_words_keeps_the_list():
    assert LISTED.with_deny_words(["extra"]).also_target == (P,)
