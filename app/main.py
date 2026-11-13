"""X-Intel FastAPI 入口：初始化存储、启动调度器、挂载 API 与控制台页面"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse

from . import db, engine, auth, doccollab
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
    # 协作写作内核（CRDT 房间 + WS）
    try:
        await doccollab.startup()
        print("[doccollab] 协作服务已启动（/ws/docs）")
    except Exception as e:
        print(f"[doccollab] 启动失败：{type(e).__name__}: {e}——REST 功能不受影响，实时协作不可用")
    yield
    await doccollab.shutdown()


app = FastAPI(title="X-Intel", version="1.0.0", lifespan=lifespan)
app.include_router(router)
# 协作 WebSocket（pycrdt-websocket 协议，房间名 = doc_uid；鉴权在 on_connect）
app.mount("/ws/docs", doccollab.asgi_app())


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


@app.get("/editor")
def editor_page(request: Request):
    if not _page_authed(request):
        return _no_cache(RedirectResponse("/login", status_code=302))
    return _no_cache(FileResponse(WEB_DIR / "editor.html", media_type="text/html"))


@app.get("/vendor/{fname}")
def vendor_assets(fname: str):
    """前端协作构建产物（collab.bundle.js 等，静态无敏感内容）"""
    p = (WEB_DIR / "vendor" / fname).resolve()
    try:
        p.relative_to((WEB_DIR / "vendor").resolve())
    except Exception:
        return RedirectResponse("/login", status_code=302)
    if not p.exists():
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/healthz")
def healthz():
    """健康检查：最小信息，不含配置/路径/版本细节（匿名可访问）"""
    return {"ok": True}
