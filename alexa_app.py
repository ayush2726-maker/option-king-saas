from main import app

# Install the broker-truth database bridge first. Every local-gateway fill/LTP
# heartbeat is written directly into the matching paper_trades row, removing
# dependence on later response-wrapper reconstruction.
import bot.live_gateway_db_bridge_v1  # noqa: F401

# Final LIVE response/accounting/profit-retention normalization remains after
# the direct database bridge so mobile aliases stay consistent.
import bot.live_quality_fix_v1  # noqa: F401

from alexa_option_king.multiuser_routes import router as alexa_router
from alexa_option_king.oauth_routes import router as alexa_oauth_router
from alexa_option_king.privacy_routes import router as privacy_router

app.include_router(alexa_router)
app.include_router(alexa_oauth_router)
app.include_router(privacy_router)
