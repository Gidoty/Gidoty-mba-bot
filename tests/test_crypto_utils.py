import pytest

from mba_bot.crypto_utils import DecryptionFailed, EncryptionNotConfigured, decrypt, encrypt, generate_key


def test_generate_key_produces_a_usable_key():
    key = generate_key()
    plaintext = "super-secret-api-key"
    assert decrypt(encrypt(plaintext, key=key), key=key) == plaintext


def test_round_trip_preserves_value():
    key = generate_key()
    for secret in ["abc123", "", "unicode-✓-value", "a" * 500]:
        assert decrypt(encrypt(secret, key=key), key=key) == secret


def test_encrypt_without_key_raises():
    with pytest.raises(EncryptionNotConfigured):
        encrypt("secret", key=None)


def test_decrypt_with_wrong_key_raises():
    key_a = generate_key()
    key_b = generate_key()
    ciphertext = encrypt("secret", key=key_a)
    with pytest.raises(DecryptionFailed):
        decrypt(ciphertext, key=key_b)


def test_decrypt_garbage_raises():
    key = generate_key()
    with pytest.raises(DecryptionFailed):
        decrypt("not-a-real-token", key=key)
