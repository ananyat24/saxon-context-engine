# Encrypts/decrypts OAuth refresh tokens before they're persisted to Neo4j
# (see app/graph/connectors.py's oauth_refresh_token_enc field, written by
# the "google_drive_oauth" connector type's exchange flow in
# app/api/connectors.py). A refresh token is a live credential: anyone who
# reads it can mint fresh access tokens and read that Google account's
# picked files for as long as the grant stands, so it's never written in
# plaintext, the same posture this app already takes toward tenant API keys
# and provider credentials, just enforced in code here instead of by "don't
# commit .env".
#
# Symmetric (Fernet, from the `cryptography` package) rather than anything
# fancier: this server is both the writer and the only reader, there's no
# multi-party key exchange to design for, and Fernet already gives
# authenticated encryption (tampering with a stored ciphertext fails to
# decrypt rather than silently returning garbage).
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class TokenEncryptionNotConfigured(Exception):
    """Raised instead of silently storing a refresh token in plaintext.
    See app/config.py's token_encryption_key. Callers (the OAuth exchange
    route) should treat this the same as any other "not configured on this
    server" setup gap, not a transient failure to retry."""


def _fernet() -> Fernet:
    # Re-reads settings.token_encryption_key on every call, not cached
    # across the process, deliberately, so a test monkeypatching it (or
    # an operator rotating it without a full restart, if that's ever wired
    # up) takes effect immediately rather than through a stale cache.
    # Constructing a Fernet instance is cheap; this isn't a hot path.
    key = settings.token_encryption_key
    if not key:
        raise TokenEncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY isn't set on this server -- required before any OAuth "
            "refresh token can be stored. Generate one with: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise TokenEncryptionNotConfigured(
            f"TOKEN_ENCRYPTION_KEY is set but isn't a valid Fernet key: {e}"
        ) from e


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """Raises InvalidToken if the ciphertext doesn't decrypt under the
    currently-configured key, e.g. TOKEN_ENCRYPTION_KEY was rotated since
    this was stored. Callers should surface that as "reconnect this
    connector", not retry."""
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


__all__ = ["encrypt_token", "decrypt_token", "TokenEncryptionNotConfigured", "InvalidToken"]
