from main import app
from alexa_option_king.multiuser_routes import router as alexa_router
from alexa_option_king.oauth_routes import router as alexa_oauth_router
from alexa_option_king.privacy_routes import router as privacy_router

app.include_router(alexa_router)
app.include_router(alexa_oauth_router)
app.include_router(privacy_router)
