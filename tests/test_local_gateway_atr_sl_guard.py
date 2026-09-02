import pytest

from local_gateway_agent.sitecustomize import _validate_server_atr_stop


def test_valid_server_atr_stop_is_allowed():
    sl, entry = _validate_server_atr_stop(
        {"payload": {"sl_price": 142.5, "expected_entry_price": 152.4}}
    )
    assert sl == 142.5
    assert entry == 152.4


@pytest.mark.parametrize("sl_price", [None, 0, "", -1])
def test_missing_or_nonpositive_server_atr_stop_is_blocked(sl_price):
    with pytest.raises(RuntimeError, match="INVALID_SERVER_ATR_SL"):
        _validate_server_atr_stop(
            {"payload": {"sl_price": sl_price, "expected_entry_price": 152.4}}
        )


def test_stop_at_or_above_entry_is_blocked():
    with pytest.raises(RuntimeError, match="INVALID_SERVER_ATR_SL"):
        _validate_server_atr_stop(
            {"payload": {"sl_price": 152.4, "expected_entry_price": 152.4}}
        )


def test_missing_expected_entry_is_blocked():
    with pytest.raises(RuntimeError, match="INVALID_SERVER_ENTRY_PRICE"):
        _validate_server_atr_stop({"payload": {"sl_price": 142.5}})
