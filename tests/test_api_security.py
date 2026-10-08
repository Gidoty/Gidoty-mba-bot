import pytest

from mba_bot.api.security import TokenError, create_session_token, hash_password, read_customer_id, verify_password


def test_password_hash_round_trips():
    password_hash = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", password_hash)


def test_password_hash_rejects_wrong_password():
    password_hash = hash_password("correct-horse-battery-staple")
    assert not verify_password("wrong-password", password_hash)


def test_password_hash_is_not_the_plaintext():
    assert hash_password("secret") != "secret"


def test_session_token_round_trips():
    token = create_session_token(customer_id="cust-123", secret="test-secret", algorithm="HS256", expires_minutes=60)
    assert read_customer_id(token, secret="test-secret", algorithm="HS256") == "cust-123"


def test_session_token_rejects_wrong_secret():
    token = create_session_token(customer_id="cust-123", secret="test-secret", algorithm="HS256", expires_minutes=60)
    with pytest.raises(TokenError):
        read_customer_id(token, secret="a-different-secret", algorithm="HS256")


def test_session_token_rejects_garbage():
    with pytest.raises(TokenError):
        read_customer_id("not-a-real-token", secret="test-secret", algorithm="HS256")


def test_expired_token_is_rejected():
    token = create_session_token(customer_id="cust-123", secret="test-secret", algorithm="HS256", expires_minutes=-1)
    with pytest.raises(TokenError):
        read_customer_id(token, secret="test-secret", algorithm="HS256")
