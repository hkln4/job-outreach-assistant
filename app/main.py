"""
FastAPI entrypoint. Localhost-only review UI (CLAUDE.md section 2) - the owner
drives every action here; nothing runs autonomously in the background.

Run with: uvicorn app.main:app --reload --port 8000
(or: python -m app.main)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import CV
from app.web.errors import register_error_handlers
from app.web.routes import (
    applications,
    company_detail,
    companies,
    cvs,
    dashboard,
    discovery,
    lang,
    preferences,
    profile,
)

EXEMPT_PATH_PREFIXES = ("/cvs", "/static", "/set-lang", "/docs", "/openapi.json", "/redoc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Application Outreach Assistant", lifespan=lifespan)
register_error_handlers(app)


@app.middleware("http")
async def require_at_least_one_cv(request: Request, call_next):
    """Onboarding gate: nothing else in the app is useful without a CV to attach
    to applications and ingest into the profile, so everything redirects to the
    CVs tab until at least one exists."""
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in EXEMPT_PATH_PREFIXES):
        return await call_next(request)

    db = SessionLocal()
    try:
        has_cv = db.query(CV).first() is not None
    finally:
        db.close()

    if not has_cv:
        return RedirectResponse(url="/cvs")

    return await call_next(request)


app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(dashboard.router)
app.include_router(lang.router)
app.include_router(profile.router)
app.include_router(preferences.router)
app.include_router(companies.router)
app.include_router(company_detail.router)
app.include_router(discovery.router)
app.include_router(cvs.router)
app.include_router(applications.router)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
