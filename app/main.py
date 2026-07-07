from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import router
from app.core.config import settings


app = FastAPI(title=settings.app_name)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def prototype() -> FileResponse:
    return FileResponse("prototype/index.html")
