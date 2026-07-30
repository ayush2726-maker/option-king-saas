from .angelone import AngelOneBroker
from .zerodha import ZerodhaBroker
from .upstox import UpstoxBroker
from .upstox_nearest_expiry_patch import install as install_upstox_nearest_expiry_patch
from bot.broker_expiry_guard import install as install_broker_expiry_guard


# Install before any broker session is created so AUTO paper/live entries,
# option-chain intelligence and quote routes all use the same validated resolver.
install_upstox_nearest_expiry_patch(UpstoxBroker)
install_broker_expiry_guard(AngelOneBroker, ZerodhaBroker, UpstoxBroker)


BROKER_REGISTRY = {
    "angelone": AngelOneBroker,
    "zerodha": ZerodhaBroker,
    "upstox": UpstoxBroker,
}


def get_all_brokers_info():
    return [{"id": k, "name": cls.display_name(), "free": cls.setup_guide()["free"], "api_cost": cls.setup_guide()["api_cost"], "required_fields": cls.required_fields(), "setup_guide": cls.setup_guide()} for k, cls in BROKER_REGISTRY.items()]


def get_broker_info(broker_name):
    cls = BROKER_REGISTRY.get(broker_name.lower().strip())
    if not cls: return {"error": f"Broker '{broker_name}' not supported"}
    return {"id": broker_name, "name": cls.display_name(), "free": cls.setup_guide()["free"], "api_cost": cls.setup_guide()["api_cost"], "required_fields": cls.required_fields(), "setup_guide": cls.setup_guide()}


def create_broker(broker_name, client_id, api_key, api_secret, totp_secret=None):
    cls = BROKER_REGISTRY.get(broker_name.lower().strip())
    if not cls: raise ValueError(f"Broker '{broker_name}' not supported. Supported: {list(BROKER_REGISTRY.keys())}")
    return cls(client_id=client_id, api_key=api_key, api_secret=api_secret, totp_secret=totp_secret)


def get_supported_brokers():
    return list(BROKER_REGISTRY.keys())
