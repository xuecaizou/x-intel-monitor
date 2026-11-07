"""X-Intel FastAPI 入口：初始化存储、启动调度器、挂载 API 与控制台页面"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import db, engine
from .config import load
from .scheduler import start as start_scheduler
from api.routes import router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    db.ensure_default_x_accounts()
    start_scheduler(engine.run_fetch, load()["fetch"]["interval_minutes"])
    yield


app = FastAPI(title="X-Intel", version="1.0.0", lifespan=lifespan)
app.include_router(router)


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/")
def index():
    return _no_cache(FileResponse(WEB_DIR / "index.html", media_type="text/html"))


@app.get("/help")
def help_page():
    return _no_cache(FileResponse(WEB_DIR / "help.html", media_type="text/html"))


@app.get("/arch")
def arch_page():
    return _no_cache(FileResponse(WEB_DIR / "arch.html", media_type="text/html"))


@app.get("/readme")
def readme_html():
    return _no_cache(FileResponse(BASE_DIR / "README.html", media_type="text/html"))


@app.get("/readme.md")
def readme_md():
    return _no_cache(FileResponse(BASE_DIR / "README.md", media_type="text/markdown"))


@app.get("/api/healthz")
def healthz():
    return {"ok": True}
