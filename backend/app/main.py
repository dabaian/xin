"""Viora AI · 后端入口。

单进程 FastAPI + SQLite(WAL) + 内置任务调度器，一台服务器一条命令就能跑起来。
"""
from __future__ import annotations

import logging
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import CFG
from .db import init_db
from .routers import admin, assets, auth, desktop, payment, tasks as task_router, wallet
from .security import api_limiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("studio")

app = FastAPI(title=CFG.app_name + " API", docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)

# ---------------------------------------------------------------- 中间件
# 前端与后端分开部署（前端静态站/CDN + 后端这台机器）时，浏览器会发跨域请求。
# 在 .env 里配 CORS_ORIGINS=https://你的前端域名 就会自动放开这几个来源。
# 注意：带 Cookie 的跨域请求不能用 "*"，必须写具体域名。
_CORS = CFG.cors_list
if _CORS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if _CORS == ["*"] else _CORS,
        allow_credentials=False if _CORS == ["*"] else True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time"],
        max_age=600,
    )
    log.info("已开启跨域，允许来源：%s", _CORS)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and request.method != "OPTIONS":
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))
        if not api_limiter.hit(ip or "unknown"):
            return JSONResponse({"detail": "请求太频繁，请稍后再试"}, status_code=429)
    started = time.time()
    resp = await call_next(request)
    resp.headers["X-Process-Time"] = "%.3f" % (time.time() - started)
    if path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(StarletteHTTPException)
async def http_err(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"detail": exc.detail, "code": exc.status_code},
                        status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_err(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = first.get("msg") or "参数不合法"
    loc = ".".join(str(x) for x in (first.get("loc") or [])[1:])
    return JSONResponse({"detail": "%s %s" % (loc, msg), "code": 422}, status_code=422)


# ---------------------------------------------------------------- 路由
for r in (auth.router, task_router.router, assets.router, wallet.router,
          payment.router, admin.router, desktop.router):
    app.include_router(r)


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": CFG.app_name, "env": CFG.app_env}


@app.get("/config.js")
def runtime_config():
    """前端运行时配置（前后端分离 / 换后端地址的唯一开关）。

    页面里 <script src="/config.js"></script> 放在 core.js 之前即可。
    前端完全独立部署（不走本服务）时，在静态站根目录放一个同样内容的
    config.js 文件就能对接到任意后端，不需要重新打包前端。
    """
    from .payments import gateway

    active = gateway.resolve_code()
    data = {
        "api_base": CFG.api_base,
        "app_name": CFG.app_name,
        "app_env": CFG.app_env,
        "version": CFG.desktop_version,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pay": {"active": active, "mock": active == "mock"},
        "features": {
            "captcha": bool(CFG.captcha_enabled),
            "email_code_required": bool(CFG.email_code_required),
            "register": bool(CFG.allow_register),
        },
    }
    body = "window.VIORA_CONFIG = " + json.dumps(data, ensure_ascii=False) + ";\n"
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/readyz")
def readyz():
    from .providers.muse import provider_from_row

    conn = None
    try:
        from .db import connect

        conn = connect()
        conn.execute("SELECT 1").fetchone()
        prow = conn.execute("SELECT * FROM model_providers WHERE code='muse'").fetchone()
        up = provider_from_row(prow).health() if prow else {"ok": False}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "db": False, "error": str(exc)}, status_code=503)
    finally:
        if conn:
            conn.close()
    if not up.get("ok"):
        return JSONResponse({"ok": False, "db": True, "upstream": up}, status_code=503)
    return {"ok": True, "db": True, "upstream": up}


# ---------------------------------------------------------------- 前端
FE = CFG.frontend_dir
if os.path.isdir(os.path.join(FE, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FE, "assets")), name="assets")


def _page(name: str) -> HTMLResponse:
    path = os.path.join(FE, name)
    if not os.path.isfile(path):
        return HTMLResponse("<h1>页面未找到</h1>", status_code=404)
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
def index():
    return _page("index.html")


for _route, _file in (
    ("/login", "login.html"),
    ("/register", "register.html"),
    ("/studio", "studio.html"),
    ("/tasks", "tasks.html"),
    ("/wallet", "wallet.html"),
    ("/library", "assets.html"),
    ("/admin", "admin.html"),
    ("/download", "download.html"),
):
    app.add_api_route(_route, (lambda f=_file: _page(f)), methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)


# ---------------------------------------------------------------- 生命周期
@app.on_event("startup")
def _startup():
    CFG.ensure_dirs()
    init_db()
    from .routers.payment import PendingSweeper
    from .tasks import runner

    runner.start()
    PendingSweeper().start()
    log.info("%s 已启动 · %s:%s · 数据目录 %s", CFG.app_name, CFG.host, CFG.port,
             CFG.data_dir)
    if not CFG.muse_key:
        log.warning("MUSE_KEY 未配置，生成任务会失败（请在 .env 填写号池密钥）")
    from .payments import gateway

    active = gateway.resolve_code()
    if not active:
        log.warning("没有任何可用支付通道，充值不可用（配置 ALIPAY_* 或临时开 ALIPAY_MOCK=1）")
    elif active == "mock":
        log.warning("当前是【模拟支付】通道，不会真收钱，上线前务必关掉 ALIPAY_MOCK")
    else:
        log.info("支付通道：%s", gateway.get(active).name)
