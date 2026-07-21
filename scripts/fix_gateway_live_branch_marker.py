from pathlib import Path

path = Path("bot/angel_fetcher.py")
text = path.read_text(encoding="utf-8")

if "OWNER_STATIC_IP_LOCAL_GATEWAY" not in text:
    raise RuntimeError("Live gateway helper was not inserted")

function_start = text.find("def _manage_paper_trade(")
if function_start < 0:
    raise RuntimeError("_manage_paper_trade not found")

function_end = text.find("\ndef angel_login(", function_start)
if function_end < 0:
    raise RuntimeError("angel_login boundary not found")

segment = text[function_start:function_end]
if "return _manage_live_gateway_entry(" in segment:
    print("Live gateway branch already present")
    raise SystemExit(0)

conn_marker = "    conn = get_db()\n"
conn_offset = segment.find(conn_marker)
if conn_offset < 0:
    raise RuntimeError("First paper manager database marker not found")

insert_at = function_start + conn_offset
branch = '''    if str(settings.get("trading_mode", "paper")).lower() == "live":
        return _manage_live_gateway_entry(
            user_id=user_id,
            underlying=underlying,
            price=price,
            side=side,
            score=score,
            trade_allowed=trade_allowed,
            settings=settings,
            obj=obj,
            spot_atr=spot_atr,
            market_data=market_data,
            candle_id=candle_id,
        )

'''
text = text[:insert_at] + branch + text[insert_at:]
path.write_text(text, encoding="utf-8")
print("Live gateway branch inserted by function position")
