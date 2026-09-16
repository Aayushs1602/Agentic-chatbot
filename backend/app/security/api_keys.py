"""API key generation, formatting, and hashing.

Pure functions, no database and no config — so the format is testable on its
own and the same rules apply wherever a key is minted or checked.

Format: ``sk_<env>_<43 chars>``, where the tail is 32 CSPRNG bytes in unpadded
base64url. The visible head (``sk_live_a3f2``) is stored alongside the hash so
a key can be identified in a list without being recoverable from the database.

**Why SHA-256 and not bcrypt/argon2.** Slow KDFs exist to make brute force
expensive against *low-entropy human passwords*. These keys carry 256 bits of
CSPRNG entropy, so there is no guessing attack to slow down: an attacker who
must try 2^256 candidates is not helped by the hash being fast. Meanwhile a
deliberately slow KDF would add ~100ms to every authenticated request, on the
hot path, forever. The same reasoning is why Stripe and GitHub hash API keys
this way while hashing passwords the other way.

The two properties that actually matter here are kept: the plaintext key is
never stored, and it is shown to the user exactly once at creation.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

# 32 bytes -> 43 base64url characters, unpadded.
_KEY_BYTES = 32
_PREFIX_CHARS = 4  # characters of the random tail kept for display


@dataclass(frozen=True)
class GeneratedKey:
    """A freshly minted key. `plaintext` is the only time it exists in full."""

    plaintext: str
    prefix: str
    hash: str


def generate_key(env: str = "live") -> GeneratedKey:
    """Mint a new key. The plaintext is returned once and never persisted."""
    tail = base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode().rstrip("=")
    plaintext = f"sk_{env}_{tail}"
    return GeneratedKey(
        plaintext=plaintext,
        prefix=key_prefix(plaintext),
        hash=hash_key(plaintext),
    )


def hash_key(plaintext: str) -> str:
    """SHA-256 hex of the key. This is what the database stores and matches on."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def key_prefix(plaintext: str) -> str:
    """The displayable head: ``sk_live_a3f2``.

    Enough to tell two keys apart in a list, far too little to reconstruct one.
    """
    try:
        scheme, env, tail = plaintext.split("_", 2)
    except ValueError:
        return plaintext[:12]
    return f"{scheme}_{env}_{tail[:_PREFIX_CHARS]}"


def looks_like_key(candidate: str) -> bool:
    """Cheap shape check, used to reject junk before touching the database.

    Not a security control — a well-formed key is still authenticated against
    the store. It exists so that a malformed `Authorization` header costs a
    string comparison instead of a query.
    """
    if not candidate.startswith("sk_"):
        return False
    parts = candidate.split("_", 2)
    return len(parts) == 3 and len(parts[2]) >= 16


def parse_bearer(header: str | None) -> str | None:
    """Extract a key from an `Authorization` header, or None.

    Accepts `Bearer <key>` and a bare key, because half of every API's users
    send the bare form and rejecting it produces a support ticket rather than
    a security improvement.
    """
    if not header:
        return None
    # Split on the scheme rather than slicing a fixed offset: `"Bearer ".strip()`
    # loses its trailing space, so a prefix test against the stripped string
    # misses the empty-credential case and hands back the word "Bearer" as if
    # it were the key.
    parts = header.strip().split(None, 1)
    if not parts:
        return None
    if parts[0].lower() == "bearer":
        return parts[1].strip() or None if len(parts) == 2 else None
    return header.strip() or None
