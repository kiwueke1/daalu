"""Break-glass local admin credential for an NV-CM environment."""

from __future__ import annotations

import pytest

from daalu_automation.config_manager_controller import local_admin as la


class _Row:
    def __init__(self, secrets_ciphertext=None):
        self.secrets_ciphertext = secrets_ciphertext


# ── validate ──────────────────────────────────────────────────────────


def test_defaults_username_and_generates_password():
    user, pw = la.validate(None, None)
    assert user == la.DEFAULT_ADMIN_USERNAME
    assert len(pw) >= la.MIN_PASSWORD_LEN


def test_generated_passwords_are_not_reused():
    assert la.validate(None, None)[1] != la.validate(None, None)[1]


def test_operator_choice_is_preserved():
    user, pw = la.validate("net-ops", "correct-horse-battery")
    assert (user, pw) == ("net-ops", "correct-horse-battery")


def test_username_is_trimmed_and_blank_falls_back():
    assert la.validate("  ops  ", None)[0] == "ops"
    assert la.validate("   ", None)[0] == la.DEFAULT_ADMIN_USERNAME


@pytest.mark.parametrize("bad", ["has space", "wrong/slash", "a" * 151, "€uro"])
def test_rejects_usernames_django_would_reject(bad):
    """Fail at the API boundary, not inside Nautobot's bootstrap job — there
    it surfaces as an opaque CrashLoop with no hint at the cause."""
    with pytest.raises(la.LocalAdminError):
        la.validate(bad, None)


def test_rejects_short_password():
    with pytest.raises(la.LocalAdminError):
        la.validate(None, "short")


def test_accepts_password_at_exactly_the_floor():
    pw = "x" * la.MIN_PASSWORD_LEN
    assert la.validate(None, pw)[1] == pw


# ── round trip ────────────────────────────────────────────────────────

# Deliberately shaped so no secret scanner mistakes it for a real credential —
# a fixture that looks like one trains people to wave through the alerts that
# matter. Long enough to clear MIN_PASSWORD_LEN, and nothing else.
_FAKE_PW = "not-a-real-password-just-a-test-fixture"


def test_round_trip_through_ciphertext():
    blob = la.to_ciphertext("net-ops", _FAKE_PW)
    assert la.from_row(_Row(blob)) == ("net-ops", _FAKE_PW)


def test_ciphertext_does_not_leak_plaintext():
    blob = la.to_ciphertext("net-ops", _FAKE_PW)
    assert _FAKE_PW not in blob
    assert "net-ops" not in blob


# ── legacy / hostile rows ─────────────────────────────────────────────


def test_row_with_no_bag_reports_no_password():
    """Empty password means "leave the installer's generated value alone",
    so pre-feature rows keep provisioning unchanged."""
    assert la.from_row(_Row(None)) == (la.DEFAULT_ADMIN_USERNAME, "")


def test_legacy_non_json_bag_does_not_raise():
    """Rows created before this feature hold a bare random token, not JSON."""
    from daalu_automation.core.crypto import encrypt_secret

    assert la.from_row(_Row(encrypt_secret("Ai8sTokenNotJson"))) == (
        la.DEFAULT_ADMIN_USERNAME,
        "",
    )


def test_undecryptable_bag_does_not_raise():
    """A rotated SECRET_KEY must not wedge provisioning over a credential we
    can regenerate."""
    assert la.from_row(_Row("not-a-fernet-token")) == (
        la.DEFAULT_ADMIN_USERNAME,
        "",
    )


def test_json_that_is_not_an_object_does_not_raise():
    from daalu_automation.core.crypto import encrypt_secret

    assert la.from_row(_Row(encrypt_secret('["a","b"]'))) == (
        la.DEFAULT_ADMIN_USERNAME,
        "",
    )
