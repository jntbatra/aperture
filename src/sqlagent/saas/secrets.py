"""Encrypting the one thing a tenant hands over that you must not lose.

What is being protected
-----------------------
A tenant's database connection string. Not their rows — those never leave their
database — but the credential that reaches them. It is the most dangerous value
this system stores, and the only one whose disclosure is a breach of *their*
data rather than yours.

Why it is encrypted at rest and not merely access-controlled
------------------------------------------------------------
The control-plane database is the thing most likely to be dumped: it is backed
up, replicated, exported for analytics, and read by every engineer who ever
debugs a tenant problem. An encrypted DSN in a leaked backup is inert. A plain
one is an open door to a customer's production database, and they gave you that
credential on the understanding that you would be the only one holding it.

Fernet, and what it is not
--------------------------
``cryptography.fernet`` is AES-128-CBC with an HMAC-SHA256 authentication tag.
Authenticated, so a tampered ciphertext fails to decrypt rather than producing
attacker-chosen plaintext — which matters here because the plaintext is fed to
a database driver.

It is **not** key management. The key has to come from somewhere, and where it
comes from is the whole security of this module:

* **Production**: AWS KMS or Secrets Manager, fetched by the task's IAM role.
  The key is never in the image, the repository or an environment variable that
  shows up in ``docker inspect``.
* **Development**: ``SQLAGENT_SECRET_KEY`` in a gitignored ``.env``.

A missing key is a hard failure, never a silent fallback to storing plaintext.
That fallback is the shape of every "we encrypt your credentials" claim that
turns out not to be true — it works, nothing errors, and the property was lost
on the day someone forgot to set a variable.

Rotation
--------
``MultiFernet`` decrypts with any key in the list and encrypts with the first,
so rotating means prepending a new key and re-encrypting in the background. The
old key stays until nothing needs it. Designed in now because retrofitting
rotation means a migration with an outage in it.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)


class SecretsError(RuntimeError):
    """Encryption is misconfigured or a value could not be decrypted.

    Never carries the plaintext, the ciphertext or the key. An exception is the
    most likely thing in this module to end up in a log aggregator.
    """


class Cipher:
    """Encrypts and decrypts tenant credentials.

    Args:
        keys: Fernet keys, newest first. More than one only during rotation.

    Raises:
        SecretsError: No keys, or a key that is not a valid Fernet key. Raised
            at construction — at startup — rather than on the first tenant to
            connect, because an application that boots and then cannot decrypt
            anything is harder to diagnose than one that refuses to boot.
    """

    def __init__(self, keys: list[str] | tuple[str, ...]) -> None:
        usable = [key.strip() for key in keys if key and key.strip()]
        if not usable:
            raise SecretsError(
                "no encryption key configured; set SQLAGENT_SECRET_KEY "
                "(generate one with Cipher.generate_key())"
            )

        try:
            self._fernet = MultiFernet([Fernet(key.encode("utf-8")) for key in usable])
        except Exception as exc:  # noqa: BLE001 - re-raised without the key
            raise SecretsError(f"encryption key is not usable: {type(exc).__name__}") from None

        self._key_count = len(usable)

    @staticmethod
    def generate_key() -> str:
        """A new Fernet key, for an operator to store in KMS or a .env.

        Exposed as a helper because the alternative is everyone finding a
        different incantation on the internet, and some of those produce keys
        with less entropy than Fernet expects.
        """
        return Fernet.generate_key().decode("utf-8")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt with the newest key.

        An empty string is rejected rather than encrypted. A tenant row holding
        the ciphertext of "" is indistinguishable from one holding a real
        credential until something tries to connect, which turns a
        configuration mistake into a runtime failure on a customer's request.
        """
        if not plaintext:
            raise SecretsError("refusing to encrypt an empty value")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt with whichever key works.

        Raises:
            SecretsError: The value was not produced by any configured key, or
                it has been tampered with. The two are deliberately not
                distinguished — the caller can do nothing different about
                either, and saying which one leaks whether a guessed key was
                close.
        """
        if not ciphertext:
            raise SecretsError("nothing to decrypt")
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise SecretsError(
                "could not decrypt; the value was encrypted with a key that is "
                "no longer configured, or it has been altered"
            ) from None

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt an existing value under the newest key.

        Run in the background after prepending a key. Once nothing decrypts
        with the old one, it can be removed.
        """
        try:
            return self._fernet.rotate(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise SecretsError("could not rotate; value does not decrypt") from None

    @property
    def keys_configured(self) -> int:
        """How many keys are loaded. For a health endpoint, never the keys."""
        return self._key_count


def redact_dsn(dsn: str) -> str:
    """A connection string safe to log or show back to a user.

    ``postgresql://user:hunter2@db.example.com:5432/sales`` becomes
    ``postgresql://user:***@db.example.com:5432/sales``.

    Exists because the useful thing to show a tenant configuring a connection
    is *which* database they pointed at — host, port, name and user are how
    they recognise it — and the password is the one part that must never come
    back out. Without a helper, someone eventually logs the whole string while
    debugging exactly that screen.
    """
    if not dsn:
        return ""

    scheme, separator, rest = dsn.partition("://")
    if not separator or "@" not in rest:
        return dsn

    credentials, _, host = rest.rpartition("@")
    user, has_password, _ = credentials.partition(":")
    if not has_password:
        return dsn
    return f"{scheme}://{user}:***@{host}"
