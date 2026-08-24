from main import app
from alexa_option_king.routes import router as alexa_router

app.include_router(alexa_router)
