from pathlib import Path

from bot.canonical_cooldown_dedup_patch import (
    CANONICAL_REASON,
    CANONICAL_SECONDS,
    DUPLICATE_REASONS,
    _filter_active_blocks,
)


def test_only_one_15_minute_same_side_cooldown_is_canonical():
    assert CANONICAL_REASON == "POST_ATR_SL_SAME_SIDE_COOLDOWN_15M"
    assert CANONICAL_SECONDS == 15 * 60
    assert CANONICAL_REASON not in DUPLICATE_REASONS


def test_known_overlapping_cooldowns_are_filtered():
    blocks = {
        ("NIFTY", "PE"): {
            "reason": "POST_RISK_EXIT_SAME_INDEX_COOLDOWN_20M_AFTER_2"
        },
        ("BANKNIFTY", "CE"): {"reason": CANONICAL_REASON},
        ("SENSEX", "PE"): {"reason": "UNRELATED_MANUAL_RISK_HOLD"},
    }

    result = _filter_active_blocks(blocks)

    assert ("NIFTY", "PE") not in result
    assert ("BANKNIFTY", "CE") in result
    assert ("SENSEX", "PE") in result


def test_global_consecutive_loss_cooldown_is_not_installed_from_main():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "apply_consecutive_loss_cooldown_patch" not in source
    assert "apply_canonical_cooldown_dedup_patch()" in source
