from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse

from app.api.routes import router
from app.adapters.workflow import AgentSpanApprovalWorkflow
from app.core.config import settings
from app.db.session import create_all


app = FastAPI(title=settings.app_name)
app.include_router(router)


@app.on_event("startup")
def initialize_database() -> None:
    create_all()
    AgentSpanApprovalWorkflow.start_worker()


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


@app.get("/")
def prototype() -> FileResponse:
    return FileResponse("prototype/index.html")
