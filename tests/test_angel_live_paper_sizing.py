import sys
import types


sys.modules.setdefault("SmartApi", types.SimpleNamespace(SmartConnect=object))
sys.modules.setdefault("pyotp", types.SimpleNamespace(TOTP=object))
sys.modules.setdefault("requests", types.SimpleNamespace())


def test_gateway_v2_executes_server_sized_complete_lots(monkeypatch):
    from local_gateway_agent import okai_local_gateway_v2 as gateway_v2

    captured = {}

    def fake_execute(self, command):
        captured.update(command["payload"])
        return {"success": True}

    monkeypatch.setattr(
        gateway_v2.base.GatewayRunner,
        "execute_entry",
        fake_execute,
    )
    runner = object.__new__(gateway_v2.RiskV2GatewayRunner)
    command = {
        "payload": {
            "quantity": 195,
            "lot_size": 65,
            "lots": 1,
        }
    }

    result = runner.execute_entry(command)

    assert result["success"] is True
    assert captured["quantity"] == 195
    assert captured["lots"] == 3


def test_gateway_v2_rejects_fractional_lot_quantity():
    from local_gateway_agent import okai_local_gateway_v2 as gateway_v2

    runner = object.__new__(gateway_v2.RiskV2GatewayRunner)
    command = {"payload": {"quantity": 100, "lot_size": 65}}

    try:
        runner.execute_entry(command)
    except RuntimeError as exc:
        assert "not a complete 65-unit lot" in str(exc)
    else:
        raise AssertionError("Fractional lot quantity was not rejected")
