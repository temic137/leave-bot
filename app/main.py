from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text

from app.api.routes import router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.models import DurableJob
from app.db.session import SessionLocal
from app.services.jobs import DurableJobWorker


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = DurableJobWorker()
    if settings.job_worker_enabled:
        worker.start()
    app.state.job_worker = worker
    try:
        yield
    finally:
        if settings.job_worker_enabled:
            worker.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def protect_admin_routes(request: Request, call_next):
    protected = request.url.path.startswith(("/admin", "/prototype"))
    if settings.app_env == "production" and protected:
        if not settings.admin_api_key:
            return JSONResponse({"detail": "ADMIN_API_KEY is not configured"}, status_code=503)
        if request.headers.get("x-admin-api-key") != settings.admin_api_key:
            return JSONResponse({"detail": "Not authorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict:
    missing = []
    if settings.app_env == "production":
        missing = [
            name
            for name, value in (
                ("SLACK_BOT_TOKEN", settings.slack_bot_token),
                ("SLACK_SIGNING_SECRET", settings.slack_signing_secret),
                ("AGENTSPAN_SERVER_URL", settings.agentspan_server_url),
            )
            if not value
        ]
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "database": str(exc)}) from exc
    if missing:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "missing_configuration": missing})
    return {"status": "ready", "database": "ok"}


@app.get("/health/dependencies")
def dependency_status() -> dict:
    try:
        with SessionLocal() as db:
            counts = dict(db.execute(select(DurableJob.status, func.count()).group_by(DurableJob.status)).all())
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "unavailable", "database": str(exc)}) from exc
    return {
        "status": "ok" if not counts.get("dead") else "degraded",
        "jobs": counts,
        "groq_configured": bool(settings.groq_api_key),
        "agentspan_configured": bool(settings.agentspan_server_url),
        "slack_configured": bool(settings.slack_bot_token),
    }


@app.get("/")
def prototype() -> FileResponse:
    return FileResponse("prototype/index.html")
