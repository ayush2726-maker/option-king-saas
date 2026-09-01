from main import app

# Keep the gateway bridge for compatibility/backfill, but LIVE display/history
# now reads Angel broker truth directly and no longer depends on paper/Upstox rows.
import bot.live_gateway_db_bridge_v1  # noqa: F401
import bot.live_quality_fix_v1  # noqa: F401
# Overlay only the authoritative runtime SL/trail fields on the Angel display.
# Price, qty, fills, P&L and costs remain Angel broker truth.
import bot.live_trail_display_bridge_v1  # noqa: F401
# AUTO Portfolio uses a separate state payload; enrich its active-position card
# with the same live Angel LTP/quantity and authoritative runtime trail SL.
import bot.auto_portfolio_live_card_sync_v1  # noqa: F401
from bot.live_mode_broker_truth_middleware import install as install_live_broker_truth
from bot.live_signal_broker_truth_middleware import install as install_live_signal_broker_truth
from bot.live_signal_auto_card_alias_v2 import install as install_live_signal_auto_card_alias

install_live_broker_truth(app)
install_live_signal_broker_truth(app)
# Must be installed after the LIVE signal authority so this layer is outermost
# and exposes the exact legacy field names consumed by the AUTO Portfolio card.
install_live_signal_auto_card_alias(app)

from alexa_option_king.multiuser_routes import router as alexa_router
from alexa_option_king.oauth_routes import router as alexa_oauth_router
from alexa_option_king.privacy_routes import router as privacy_router

app.include_router(alexa_router)
app.include_router(alexa_oauth_router)
app.include_router(privacy_router)
