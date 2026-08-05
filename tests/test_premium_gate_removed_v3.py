import inspect

from bot import entry_execution_safety_v1_patch as patch


def test_disabled_premium_snapshot_never_blocks():
    snapshot = patch._premium_gate_disabled(
        101.25,
        {
            "allowed": False,
            "reason": "OPTION_SPIKE_REVERSING",
        },
    )
    assert snapshot["allowed"] is True
    assert snapshot["gate_enabled"] is False
    assert snapshot["reason"] == "OPTION_PREMIUM_GATE_DISABLED"
    assert snapshot["observed_quality_allowed"] is False


def test_final_execution_wrapper_has_no_premium_block():
    source = inspect.getsource(
        patch.apply_entry_execution_safety_v1_patch
    )
    assert 'not quality_copy.get("allowed", True)' not in source
    assert 'not momentum.get("allowed")' not in source
    assert 'or option_candle_health.get("blocked")' not in source
    assert "momentum = _premium_gate_disabled(" in source
