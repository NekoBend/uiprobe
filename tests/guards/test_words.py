"""Tests for the default deny and confirm word lists."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.guards.helpers import (
    BASE_DENY,
    DIALOG_ROOT,
    EXPECTED_CONFIRM,
    EXTRA_DENY,
    SETTINGS_DENY,
    SHOP_DENY,
    STATE_DENY,
    el,
    reasons,
    run,
)
from uiprobe.guards import (
    CONFIRM_WORDS,
    DEFAULT_DENY_WORDS,
    word_matches,
)

# -- lists and policy ------------------------------------------------------------------


def test_default_lists_hold_expected_words() -> None:
    for word in ("削除", "ログアウト", "sign up", "log out", "check out", "無料体験"):
        assert word in DEFAULT_DENY_WORDS
    for word in ("OK", "はい", "yes", "done", "許可"):
        assert word in CONFIRM_WORDS
    assert len(set(DEFAULT_DENY_WORDS)) == len(DEFAULT_DENY_WORDS)


def test_default_word_lists_match_the_expected_lists() -> None:
    assert len(BASE_DENY) == 23 + 23
    assert len(SETTINGS_DENY) == 9
    assert len(SHOP_DENY) == 4
    assert len(STATE_DENY) == 16
    assert DEFAULT_DENY_WORDS[-29:-20] == tuple(SETTINGS_DENY)
    assert DEFAULT_DENY_WORDS[-20:-16] == tuple(SHOP_DENY)
    assert DEFAULT_DENY_WORDS[-16:] == tuple(STATE_DENY)
    assert Counter(DEFAULT_DENY_WORDS) == Counter(
        BASE_DENY + EXTRA_DENY + SETTINGS_DENY + SHOP_DENY + STATE_DENY
    )
    assert Counter(CONFIRM_WORDS) == Counter(EXPECTED_CONFIRM)


@pytest.mark.parametrize(
    "word", BASE_DENY + EXTRA_DENY + SETTINGS_DENY + SHOP_DENY + STATE_DENY
)
def test_every_deny_word_refuses_a_button(word) -> None:
    found = run([el("e2", text=word)])
    assert "deny_word" in reasons(found, "e2")


def test_clock_app_info_page_refuses_disable_and_force_stop() -> None:
    found = run(
        [
            el("e2", text="Open", bounds=(10, 100, 100, 150)),
            el("e3", text="Disable", bounds=(110, 100, 200, 150)),
            el("e4", text="Force stop", bounds=(210, 100, 300, 150)),
        ]
    )
    assert reasons(found, "e2") == ()
    assert "deny_word" in reasons(found, "e3")
    assert "deny_word" in reasons(found, "e4")


@pytest.mark.parametrize(
    "label",
    ["Sign in", "Log in", "LOGIN", "ログイン", "サインイン", "無効にする", "強制停止"],
)
def test_sign_in_and_disable_labels_are_refused(label) -> None:
    found = run([el("e2", text=label)])
    assert "deny_word" in reasons(found, "e2")


@pytest.mark.parametrize(
    ("label", "word"),
    [
        ("Signing out", "sign in"),
        ("Catalog in stock", "log in"),
        ("Log info", "log in"),
        ("Dialog input", "log in"),
        ("Design info", "sign in"),
        ("Sign inbox", "sign in"),
        ("ブログインポート", "ログイン"),
        ("Disabled", "disable"),
        ("Wi-Fi 無効", "無効"),
    ],
)
def test_dry_run_words_over_block_as_documented(label, word) -> None:
    assert word_matches(label, word)
    found = run([el("e2", text=label)])
    assert "deny_word" in reasons(found, "e2")


def test_disabled_status_text_does_not_match_the_japanese_word() -> None:
    assert not word_matches("Disabled", "無効")


def test_cart_and_account_buttons_are_refused_and_products_is_not() -> None:
    found = run(
        [
            el("e2", text="Add to cart", bounds=(10, 100, 100, 150)),
            el("e3", text="My account", bounds=(110, 100, 200, 150)),
            el("e4", text="カートに入れる", bounds=(210, 100, 300, 150)),
            el("e5", text="アカウント", bounds=(10, 160, 100, 210)),
            el("e6", text="Products", bounds=(110, 160, 200, 210)),
        ]
    )
    for ident in ("e2", "e3", "e4", "e5"):
        assert reasons(found, ident) == ("deny_word",)
    assert reasons(found, "e6") == ()


@pytest.mark.parametrize(
    ("label", "word"),
    [
        ("Accounts", "account"),
        ("Accounting", "account"),
        ("Cartoon", "cart"),
        ("Cartridge", "cart"),
        ("Music artists", "cart"),
        ("Comic art", "cart"),
        ("Electric Artwork", "cart"),
        ("Ac count", "account"),
        ("カートゥーン", "カート"),
        ("アカウント情報", "アカウント"),
    ],
)
def test_cart_and_account_words_over_block_as_documented(label, word) -> None:
    assert word_matches(label, word)
    found = run([el("e2", text=label)])
    assert "deny_word" in reasons(found, "e2")


@pytest.mark.parametrize("word", EXPECTED_CONFIRM)
def test_every_confirm_word_is_a_dialog_confirm_button(word) -> None:
    found = run([el("e2", text=word)], root_bounds=DIALOG_ROOT)
    assert "dialog_confirm_button" in reasons(found, "e2")


@pytest.mark.parametrize("word", ["restart", "undo", "やり直", "元に戻"])
def test_the_start_over_words_are_default_deny_words(word) -> None:
    assert word in DEFAULT_DENY_WORDS


@pytest.mark.parametrize(
    "label", ["Restart", "UNDO", "Undo last edit", "もう一度やり直す", "元に戻す"]
)
def test_start_over_labels_are_refused(label) -> None:
    assert "deny_word" in reasons(run([el("e2", text=label)]), "e2")
