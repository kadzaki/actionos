import os
import uvicorn

from .config import Settings

settings = Settings.from_env()
uvicorn.run("app.main:app", host=os.getenv("APP_HOST", "127.0.0.1"),
            port=settings.app_port, workers=1, reload=False)
