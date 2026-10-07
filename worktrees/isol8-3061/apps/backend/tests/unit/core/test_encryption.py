"""Direct contract tests for core.encryption (Fernet encrypt/decrypt).

These were previously inlined in test_key_service.py. key_service no longer
imports core.encryption (its BYOK secrets moved to Secrets Manager), but the
module is still used by the OAuth + Paperclip services, so its round-trip
contract keeps its own coverage here.
"""

import pytest

from core import encryption
from core.encryption import decrypt, encrypt, get_fernet


def test_encrypt_decrypt_roundtrip():
    plaintext = "sk-secret-value-123"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext  # actually encrypted, not passed through
    assert decrypt(ciphertext) == plaintext


def test_encrypt_is_nondeterministic():
    # Fernet embeds a random IV + timestamp, so two encryptions of the same
    # plaintext differ, yet both decrypt back to it.
    a = encrypt("same")
    b = encrypt("same")
    assert a != b
    assert decrypt(a) == decrypt(b) == "same"


def test_decrypt_garbage_raises_valueerror():
    with pytest.raises(ValueError):
        decrypt("not-a-valid-token")


def test_get_fernet_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError):
        get_fernet()


def test_passphrase_style_key_is_derived(monkeypatch):
    # A non-Fernet raw value is HKDF-derived into a valid key and round-trips.
    monkeypatch.setattr(encryption.settings, "ENCRYPTION_KEY", "any-passphrase-value")
    token = encrypt("hello")
    assert decrypt(token) == "hello"
