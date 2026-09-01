from main import app

# Final LIVE wiring/accounting/profit-retention patch is installed after main
# so it remains authoritative over earlier compatibility wrappers.
import bot.live_quality_fix_v1  # noqa: F401

from alexa_option_king.multiuser_routes import router as alexa_router
from alexa_option_king.oauth_routes import router as alexa_oauth_router
from alexa_option_king.privacy_routes import router as privacy_router

app.include_router(alexa_router)
app.include_router(alexa_oauth_router)
app.include_router(privacy_router)
