import importlib.util
import sys
import types

# Pure smoke test: no database or network required.
database = types.ModuleType("database")
database.get_db = lambda: None
database.get_db_storage_info = lambda: {"persistent": True}
sys.modules["database"] = database

bot = types.ModuleType("bot")
shared = types.ModuleType("bot.shared_ai")
shared.predict = lambda snapshot: {}
sys.modules["bot"] = bot
sys.modules["bot.shared_ai"] = shared

spec = importlib.util.spec_from_file_location(
    "news_intelligence",
    "bot/news_intelligence.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

war = module.classify_headline(
    "Missile attack escalates war and crude oil supply disruption",
    source_type="GDELT_GLOBAL",
    domain="reuters.com",
)
assert war["direction"] == "PE", war
assert war["high_impact"] is True, war

peace = module.classify_headline(
    "Ceasefire and peace deal reached after talks",
    source_type="GDELT_GLOBAL",
    domain="reuters.com",
)
assert peace["direction"] == "CE", peace

base = {
    "decision": "CE",
    "confidence": 78,
    "probabilities": {"CE": 70, "PE": 10, "NO_TRADE": 20},
}
news = {
    "fresh": True,
    "news_bias": "PE",
    "news_strength": 90,
    "news_risk_score": 88,
    "event_count": 8,
    "high_impact_count": 3,
}
market = {
    "price": 25000,
    "ema_fast": 24980,
    "ema_slow": 25010,
    "vwap": 25020,
    "signal_direction": "PE",
}
fusion = module.fuse_news_with_market(base, news, market)
assert fusion["decision"] in {"PE", "NO_TRADE"}, fusion
assert fusion["market_reaction"] == "NEWS_MARKET_REACTION_CONFIRMED"
assert fusion["trade_blocking"] is False
assert fusion["order_execution"] is False

print("PASS OKAI-NEWS-FUSION-SHADOW-V1")
