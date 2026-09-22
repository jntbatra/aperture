"""Hashing user passwords.

Why this is not the API-key hash
--------------------------------
:func:`sqlagent.saas.tenancy.hash_secret` is a plain SHA-256, and that is
correct there: an API key is 256 bits of randomness this system generated, so
there is no smaller search space for a work factor to protect.

A password is the opposite. It is chosen by a person, it is short, it is
probably reused, and it is very likely in a list someone already has. The
entire defence is making each guess expensive, so the same reasoning that
argues *against* a KDF for API keys argues *for* one here. Getting these two
the same way round is a common and expensive mistake.

Why scrypt
----------
``hashlib.scrypt`` is in the standard library, and it is memory-hard: an
attacker with a GPU farm cannot trade memory for parallelism the way they can
against PBKDF2. Argon2id is the modern first choice and would mean a
dependency; scrypt is the strongest thing available without one and is a long
way past "good enough" at these parameters.

Why the parameters live in the stored string
--------------------------------------------
``scrypt$n$r$p$salt$hash``. The cost parameters are stored per hash rather than
read from a constant, so raising them later does not invalidate every existing
password. Old hashes keep verifying under their own parameters, and
:func:`needs_rehash` says which ones to upgrade the next time their owner
signs in — which is the only moment the plaintext is available to rehash with.

A scheme that reads its parameters from a global constant cannot raise them
without locking everybody out, so in practice it never raises them.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SCRYPT_N = 2**15
"""CPU/memory cost. 32,768 puts a single hash at roughly 100ms and 32MB.

Chosen to be slow enough to matter against offline guessing and fast enough
that a sign-in does not feel broken. It is a tunable, not a constant of nature:
raise it as hardware improves, and `needs_rehash` will migrate people.
"""

SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

MIN_LENGTH = 12
"""Minimum password length.

Length only. No "must contain a symbol": composition rules push people toward
``Password1!`` — which is in every list — and away from a long passphrase,
which is not. The advice that survives contact with evidence is *longer*.
"""


def _maxmem(n: int, r: int) -> int:
    """Memory scrypt must be allowed, from its own parameters.

    The algorithm needs 128 * N * r bytes; OpenSSL rejects anything at or below
    that exactly, so this asks for double. Derived rather than written as a
    constant because the two must move together — a raised N with a stale
    memory limit fails at runtime with an error that says nothing about the
    cause.
    """
    return 128 * n * r * 2


class PasswordError(ValueError):
    """The password is unusable. Safe to show; never contains the password."""


def validate(password: str) -> None:
    """Raise if this password cannot be accepted.

    Deliberately thin. The server is the only place this can be enforced — a
    rule in the browser is a rule with an edit button — and duplicating it in
    both means two rules that eventually disagree.
    """
    if len(password or "") < MIN_LENGTH:
        raise PasswordError(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > 1024:
        # Not a strength rule. An unbounded input to a deliberately slow,
        # memory-hard function is a denial-of-service vector: a megabyte
        # password would tie up a worker for a long time, for free.
        raise PasswordError("That password is too long.")


def hash_password(password: str) -> str:
    """Hash a password for storage. Returns ``scrypt$n$r$p$salt$hash``."""
    validate(password)
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
        # scrypt refuses unless it is allowed this much memory. Sized from the
        # parameters rather than hardcoded, so raising N produces a slower hash
        # rather than a confusing "memory limit exceeded". The 2x headroom is
        # what OpenSSL wants above the theoretical 128*N*r.
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R),
    )
    return "$".join(
        ["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), salt.hex(), derived.hex()]
    )


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` produces ``stored``. Never raises.

    A malformed stored value is False, not an exception: a corrupt row should
    fail one sign-in, not return a 500 that tells the caller the row is
    corrupt.
    """
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError, MemoryError):
        return False

    # Constant-time. A plain == leaks how many leading bytes matched, which
    # over enough attempts is enough to reconstruct the hash.
    return hmac.compare_digest(derived.hex(), hash_hex)


def needs_rehash(stored: str) -> bool:
    """Whether this hash was made with weaker parameters than we now use.

    Checked at sign-in, because that is the only moment the plaintext exists to
    rehash with. Without this, raising the cost parameters protects nobody who
    already has an account — which is everybody.
    """
    try:
        scheme, n, r, p, _, _ = stored.split("$")
    except ValueError:
        return True
    return scheme != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
