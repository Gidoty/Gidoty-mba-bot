from mba_bot.exchange import WithdrawalPermissionDetected, safe_error_message
from ccxt.async_support import AuthenticationError, ExchangeError, InsufficientFunds, NetworkError


def test_authentication_error_is_categorized_without_echoing_raw_text():
    message = safe_error_message(AuthenticationError("Signature for this request is not valid. key=AbCdEf123"))
    assert "AbCdEf123" not in message
    assert "api key" in message.lower() or "api key" in message


def test_withdrawal_permission_detected_has_its_own_clear_message():
    message = safe_error_message(WithdrawalPermissionDetected("stored API key has withdrawal permission enabled"))
    assert "withdrawal" in message.lower()


def test_network_error_is_framed_as_temporary():
    message = safe_error_message(NetworkError("Connection reset by peer at 10.0.4.2:443"))
    assert "10.0.4.2" not in message
    assert "temporary" in message.lower() or "try again" in message.lower()


def test_insufficient_funds_is_a_distinct_category():
    message = safe_error_message(InsufficientFunds("Account has insufficient balance for requested action."))
    assert "balance" in message.lower()


def test_generic_exchange_error_does_not_leak_raw_text():
    secret_looking_text = "Bad Request: apiSecret=sk_live_super_secret_value_1234567890"
    message = safe_error_message(ExchangeError(secret_looking_text))
    assert "sk_live_super_secret_value_1234567890" not in message


def test_unknown_exception_type_falls_back_to_a_generic_message():
    message = safe_error_message(ValueError("some internal detail nobody outside should see"))
    assert "some internal detail" not in message
    assert message
