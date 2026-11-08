"""X-Intel FastAPI 入口：初始化存储、启动调度器、挂载 API 与控制台页面"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse

from . import db, engine, auth
from .config import load
from .scheduler import start as start_scheduler
from api.routes import router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    db.ensure_default_x_accounts()
    auth.init_auth_tables()
    msg = auth.try_consume_init_file()
    if msg:
        print("[auth]", msg)
    auth.seed_initial_admin_if_needed()
    cfg0 = load()
    if (cfg0.get("scheduler") or {}).get("enabled", True):
        start_scheduler(engine.run_fetch, cfg0["fetch"]["interval_minutes"])
    else:
        print("[scheduler] 已按配置暂停（scheduler.enabled=false）——恢复/演练场景不应自动抓取与外发")
    yield


app = FastAPI(title="X-Intel", version="1.0.0", lifespan=lifespan)
app.include_router(router)


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/")
def index(request: Request):
    if not _page_authed(request):
        return _no_cache(RedirectResponse("/login", status_code=302))
    return _no_cache(FileResponse(WEB_DIR / "index.html", media_type="text/html"))


@app.get("/help")
def help_page(request: Request):
    if not _page_authed(request):
        return _no_cache(RedirectResponse("/login", status_code=302))
    return _no_cache(FileResponse(WEB_DIR / "help.html", media_type="text/html"))


@app.get("/arch")
def arch_page(request: Request):
    if not _page_authed(request):
        return _no_cache(RedirectResponse("/login", status_code=302))
    return _no_cache(FileResponse(WEB_DIR / "arch.html", media_type="text/html"))


@app.get("/readme")
def readme_html():
    return _no_cache(FileResponse(BASE_DIR / "README.html", media_type="text/html"))


@app.get("/readme.md")
def readme_md():
    return _no_cache(FileResponse(BASE_DIR / "README.md", media_type="text/markdown"))


def _page_authed(request: Request) -> bool:
    """页面鉴权：auth 关闭时放行；否则需要会话或 Bearer Token"""
    # 仅测试模式允许关闭认证（生产误配按启用处理，见 app/auth._auth_disabled_allowed）
    if auth._auth_disabled_allowed():
        return True
    if not auth.init_ready():
        return False
    sid = request.cookies.get(auth.COOKIE_NAME, "")
    if auth.get_session(sid):
        return True
    a = request.headers.get("authorization", "")
    if a.lower().startswith("bearer ") and auth.verify_token(a[7:].strip()):
        return True
    return False


@app.get("/login")
def login_page():
    return _no_cache(FileResponse(WEB_DIR / "login.html", media_type="text/html"))


@app.get("/api/healthz")
def healthz():
    """健康检查：最小信息，不含配置/路径/版本细节（匿名可访问）"""
    return {"ok": True}
