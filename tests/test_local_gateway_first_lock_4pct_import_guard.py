def test_gateway_first_lock_module_imports():
    from local_gateway_agent import okai_local_gateway_v2 as v2
    assert v2.FIRST_LOCK_NET_PERCENT == 4.0
