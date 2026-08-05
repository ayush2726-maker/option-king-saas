from pathlib import Path

from bot import auto_portfolio_runtime as runtime


def test_old_two_position_cap_removed():
    assert runtime.MAX_OPEN_POSITIONS == len(runtime.ALLOWED_INSTRUMENTS)
    assert runtime.MAX_OPEN_POSITIONS == 3
    assert runtime.SLOT_ALLOCATIONS[3] == 0.0


def test_paper_stale_and_dns_guards_are_audit_only():
    source = Path("bot/entry_execution_safety_v1_patch.py").read_text(encoding="utf-8")
    assert 'if current_mode == "live"' in source
    assert '"stale_candle_block_disabled": current_mode == "paper"' in source
    assert '"broker_health_block_disabled": current_mode == "paper"' in source


def test_contract_and_quote_have_retries_without_fabricated_price():
    source = Path("bot/auto_portfolio_runtime.py").read_text(encoding="utf-8")
    assert 'for expiry_request in ("current_week", "nearest", None)' in source
    assert 'for _quote_attempt in range(3)' in source
    assert 'OPTION_LTP_FAILED' in source
