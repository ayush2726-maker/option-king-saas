from pathlib import Path


def test_final_execution_guard_persists_visible_reason():
    source = Path("bot/entry_execution_safety_v1_patch.py").read_text(encoding="utf-8")
    assert '"stage": "FINAL_EXECUTION_GUARD"' in source
    assert 'state["last_entry_attempt"] = dict(attempt)' in source
    assert 'state["entry_block_reason"] = reason' in source


def test_signal_api_exposes_entry_reason():
    source = Path("bot/routes.py").read_text(encoding="utf-8")
    assert '"entry_guard": (' in source
    assert '"entry_attempt": (' in source
    assert '"entry_block_reason": (' in source
