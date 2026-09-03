# app/graph/token_crypto.py: encrypts OAuth refresh tokens before they're
# stored in Neo4j (see the "google_drive_oauth" connector type). No real
# Neo4j or network needed, just settings.token_encryption_key.
import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.graph.token_crypto import TokenEncryptionNotConfigured, decrypt_token, encrypt_token


def test_round_trips_a_token(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    encrypted = encrypt_token("a-real-refresh-token")
    assert encrypted != "a-real-refresh-token"
    assert decrypt_token(encrypted) == "a-real-refresh-token"


def test_encrypting_without_a_key_configured_raises_clearly(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", "")
    with pytest.raises(TokenEncryptionNotConfigured, match="TOKEN_ENCRYPTION_KEY"):
        encrypt_token("secret")


def test_a_malformed_key_raises_the_same_not_configured_error(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", "not-a-real-fernet-key")
    with pytest.raises(TokenEncryptionNotConfigured):
        encrypt_token("secret")


def test_decrypting_under_the_wrong_key_raises_invalid_token(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    encrypted = encrypt_token("secret")
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        decrypt_token(encrypted)
