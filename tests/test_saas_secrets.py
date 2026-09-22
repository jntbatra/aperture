"""Tests for tenant credential encryption.

The value being protected is a customer's database connection string — the most
dangerous thing this system stores, and the only one whose disclosure is a
breach of *their* data rather than yours.
"""

from __future__ import annotations

import pytest

from sqlagent.saas.secrets import Cipher, SecretsError, redact_dsn

DSN = "postgresql+psycopg://reader:hunter2@db.example.com:5432/sales"


@pytest.fixture
def cipher() -> Cipher:
    return Cipher([Cipher.generate_key()])


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_a_credential_survives_the_round_trip(cipher):
    assert cipher.decrypt(cipher.encrypt(DSN)) == DSN


def test_the_ciphertext_does_not_contain_the_password(cipher):
    """The whole point. An encrypted DSN in a leaked backup is inert."""
    assert "hunter2" not in cipher.encrypt(DSN)


def test_the_ciphertext_does_not_contain_the_host(cipher):
    """Not merely the password — which customer's database it is should not be
    readable either."""
    assert "db.example.com" not in cipher.encrypt(DSN)


def test_encrypting_twice_gives_different_ciphertext(cipher):
    """Fernet carries a random IV. Identical ciphertexts would let anyone with
    the table see which tenants share a database."""
    assert cipher.encrypt(DSN) != cipher.encrypt(DSN)


# --------------------------------------------------------------------------
# Failing closed
# --------------------------------------------------------------------------


def test_no_key_is_a_hard_failure(): 
    """Never a silent fallback to plaintext. That fallback is the shape of
    every "we encrypt your credentials" claim that turns out not to be true —
    it works, nothing errors, and the property was lost the day someone forgot
    to set a variable."""
    with pytest.raises(SecretsError, match="no encryption key"):
        Cipher([])


def test_blank_keys_count_as_no_key():
    """`SQLAGENT_SECRET_KEY=` in a .env is an unset key, not an empty one."""
    with pytest.raises(SecretsError):
        Cipher(["", "   "])


def test_a_malformed_key_fails_at_construction():
    """At startup, not on the first tenant to connect. An application that
    boots and then cannot decrypt anything is harder to diagnose than one that
    refuses to boot."""
    with pytest.raises(SecretsError, match="not usable"):
        Cipher(["not-a-fernet-key"])


def test_the_error_does_not_contain_the_key():
    """An exception is the most likely thing here to reach a log aggregator."""
    try:
        Cipher(["sekrit-looking-but-invalid"])
    except SecretsError as error:
        assert "sekrit" not in str(error)


def test_an_empty_value_is_not_encrypted(cipher):
    """A row holding the ciphertext of "" is indistinguishable from a real
    credential until something tries to connect."""
    with pytest.raises(SecretsError):
        cipher.encrypt("")


def test_decrypting_nothing_is_an_error(cipher):
    with pytest.raises(SecretsError):
        cipher.decrypt("")


def test_a_value_from_another_key_does_not_decrypt():
    other = Cipher([Cipher.generate_key()])
    mine = Cipher([Cipher.generate_key()])

    with pytest.raises(SecretsError):
        mine.decrypt(other.encrypt(DSN))


def test_a_tampered_ciphertext_is_rejected_not_mangled(cipher):
    """Authenticated encryption. The plaintext is fed to a database driver, so
    attacker-chosen plaintext would be attacker-chosen connection target."""
    blob = cipher.encrypt(DSN)
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")

    with pytest.raises(SecretsError):
        cipher.decrypt(tampered)


def test_the_decrypt_error_does_not_say_which_failure_it_was():
    """Wrong key and tampered value are not distinguished: the caller can do
    nothing different about either, and saying which leaks whether a guessed
    key was close."""
    mine = Cipher([Cipher.generate_key()])
    other = Cipher([Cipher.generate_key()])
    blob = other.encrypt(DSN)

    messages = set()
    for value in (blob, blob[:-4] + "AAAA"):
        try:
            mine.decrypt(value)
        except SecretsError as error:
            messages.add(str(error))

    assert len(messages) == 1


# --------------------------------------------------------------------------
# Rotation
#
# Designed in now because retrofitting it means a migration with an outage.
# --------------------------------------------------------------------------


def test_an_old_key_still_decrypts_during_rotation():
    old_key = Cipher.generate_key()
    new_key = Cipher.generate_key()
    blob = Cipher([old_key]).encrypt(DSN)

    rotating = Cipher([new_key, old_key])

    assert rotating.decrypt(blob) == DSN


def test_rotation_re_encrypts_under_the_newest_key():
    old_key = Cipher.generate_key()
    new_key = Cipher.generate_key()
    blob = Cipher([old_key]).encrypt(DSN)

    rotated = Cipher([new_key, old_key]).rotate(blob)

    assert Cipher([new_key]).decrypt(rotated) == DSN


def test_a_rotated_value_no_longer_needs_the_old_key():
    """The point of rotating: once nothing decrypts with the old key, it can be
    removed."""
    old_key, new_key = Cipher.generate_key(), Cipher.generate_key()
    rotated = Cipher([new_key, old_key]).rotate(Cipher([old_key]).encrypt(DSN))

    with pytest.raises(SecretsError):
        Cipher([old_key]).decrypt(rotated)


def test_new_values_use_the_newest_key():
    old_key, new_key = Cipher.generate_key(), Cipher.generate_key()

    blob = Cipher([new_key, old_key]).encrypt(DSN)

    assert Cipher([new_key]).decrypt(blob) == DSN


def test_generated_keys_are_unique():
    assert Cipher.generate_key() != Cipher.generate_key()


def test_the_key_count_is_visible_but_the_keys_are_not():
    """For a health endpoint: "encryption configured, 2 keys" is useful and
    safe; the keys are neither."""
    keys = [Cipher.generate_key(), Cipher.generate_key()]
    cipher = Cipher(keys)

    assert cipher.keys_configured == 2
    assert all(key not in repr(cipher) for key in keys)


# --------------------------------------------------------------------------
# Showing a DSN back to the person who configured it
# --------------------------------------------------------------------------


def test_the_password_is_removed():
    assert redact_dsn(DSN) == "postgresql+psycopg://reader:***@db.example.com:5432/sales"


def test_what_the_tenant_needs_to_recognise_it_is_kept():
    """Host, port, database and user are how someone identifies which database
    they pointed at. Redacting those makes the screen useless."""
    redacted = redact_dsn(DSN)

    for fragment in ("db.example.com", "5432", "sales", "reader"):
        assert fragment in redacted


def test_a_dsn_with_no_password_is_unchanged():
    dsn = "postgresql://reader@db.example.com:5432/sales"

    assert redact_dsn(dsn) == dsn


def test_a_dsn_with_no_credentials_is_unchanged():
    dsn = "sqlite:///local.db"

    assert redact_dsn(dsn) == dsn


def test_an_empty_dsn_redacts_to_empty():
    assert redact_dsn("") == ""


def test_a_password_containing_an_at_sign_is_still_removed():
    """`rpartition` on `@`, because a password may legally contain one and
    splitting on the first would leave most of it in the output."""
    redacted = redact_dsn("postgresql://reader:p@ss@db.example.com:5432/sales")

    assert "p@ss" not in redacted
    assert "db.example.com" in redacted
