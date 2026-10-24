"""应用入口。"""
from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import accounts, api, sensitive, settings
from .config import FINISH_DIR, OUTPUT_DIR, WEB_DIR
from .db import init_db
from .fetcher import engine
from .media.collector import engine as media_engine
from .publish import jobs as publish_jobs
from .rewriter import engine as rewrite_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.seed_defaults()   # 预置配置项（缺失才插入，不覆盖已改的值）
    sensitive.warmup()         # 后台建敏感词自动机，别让第一篇文章等构建
    await engine.start()
    await media_engine.start()
    await rewrite_engine.start()
    await publish_jobs.start()      # 回收僵尸发布任务 + 拉起 worker
    accounts.start_scheduler()
    yield
    accounts.stop_scheduler()
    await publish_jobs.stop()
    await rewrite_engine.stop()
    await media_engine.stop()
    await engine.stop()


app = FastAPI(title="热点资讯采集台", version="0.1.0", lifespan=lifespan)
app.include_router(api.router, prefix="/api")

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
# output/<标题>/ 下的原文与素材，供页面直接预览 / 下载
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
# output_finish/<平台>/<标题>/ 下的成稿，流水线里可直接打开预览
FINISH_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/finish", StaticFiles(directory=str(FINISH_DIR)), name="finish")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常不要把原因吞掉。

    否则前端只能收到一个「Internal Server Error」，看不出到底哪里坏了，
    排查全靠翻服务端日志。
    """
    logging.getLogger(__name__).exception(
        "未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500,
                        content={"detail": f"{type(exc).__name__}: {exc}"})


@app.middleware("http")
async def _no_cache(request, call_next):
    """前端改版后必须立刻生效：禁止浏览器缓存页面与静态资源。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    path = WEB_DIR / "favicon.ico"
    return FileResponse(path) if path.exists() else FileResponse(WEB_DIR / "index.html")
