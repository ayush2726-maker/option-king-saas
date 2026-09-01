from main import app

# Keep the gateway bridge for compatibility/backfill, but LIVE display/history
# now reads Angel broker truth directly and no longer depends on paper/Upstox rows.
import bot.live_gateway_db_bridge_v1  # noqa: F401
import bot.live_quality_fix_v1  # noqa: F401
from bot.live_mode_broker_truth_middleware import install as install_live_broker_truth
from bot.live_signal_broker_truth_middleware import install as install_live_signal_broker_truth

install_live_broker_truth(app)
install_live_signal_broker_truth(app)

from alexa_option_king.multiuser_routes import router as alexa_router
from alexa_option_king.oauth_routes import router as alexa_oauth_router
from alexa_option_king.privacy_routes import router as privacy_router

app.include_router(alexa_router)
app.include_router(alexa_oauth_router)
app.include_router(privacy_router)
