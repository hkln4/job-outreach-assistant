"""
FastAPI entrypoint. Localhost-only review UI (CLAUDE.md section 2) - the owner
drives every action here; nothing runs autonomously in the background.

Run with: uvicorn app.main:app --reload --port 8000
(or: python -m app.main)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.web.routes import companies, dashboard


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Application Outreach Assistant", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(dashboard.router)
app.include_router(companies.router)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
