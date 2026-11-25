"""기능별 라우터와 정적 화면을 조립하는 FastAPI 애플리케이션."""
import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.ai.routes import router as ai_router
from app.auth.routes import router as auth_router
from app.calendar.routes import router as calendar_router
from app.chat.routes import router as chat_router
from app.core import metrics, runtime
from app.core.config import FRONTEND_DIR, LANGUAGES, RUN_MEDIA_WORKER
from app.media.routes import router as media_router
from app.rooms.routes import router as rooms_router
from app.users.routes import router as users_router

log = logging.getLogger(__name__)
APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; media-src 'self' blob: https://*.amazonaws.com https://*.amazonaws.com.cn; "
    "connect-src 'self' ws: wss: https://*.amazonaws.com https://*.amazonaws.com.cn; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
DOCS_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; font-src 'self' https://fonts.gstatic.com; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    runtime.loop = asyncio.get_running_loop()
    async def folders():
        while True:
            try:
                await asyncio.to_thread(runtime.room_folders.recover)
            except Exception:
                log.exception("Room folder recovery failed")
            await asyncio.sleep(30)

    async def stop_folders(task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    try:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(asyncio.to_thread, runtime.close_notifications)
            cleanup.push_async_callback(runtime.manager.stop)
            await runtime.manager.start()
            cleanup.push_async_callback(stop_folders, asyncio.create_task(folders()))
            if RUN_MEDIA_WORKER:
                cleanup.push_async_callback(asyncio.to_thread, runtime.media_svc.queue.stop)
                runtime.media_svc.queue.start()
            if not runtime.ai_queue_url:
                cleanup.push_async_callback(asyncio.to_thread, runtime.ai_jobs.queue.stop)
                runtime.ai_jobs.queue.start()
            stop_metrics = metrics.start("Web", lambda: runtime.manager.connection_count)
            cleanup.push_async_callback(asyncio.to_thread, stop_metrics)
            yield
    finally:
        runtime.loop = None


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
app.include_router(auth_router)
app.include_router(rooms_router)
app.include_router(media_router)
app.include_router(chat_router)
app.include_router(users_router)
app.include_router(ai_router)
app.include_router(calendar_router)


def apply_security_headers(response, path):
    csp = DOCS_CSP if path in ("/docs", "/redoc", "/docs/oauth2-redirect") else APP_CSP
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def security_headers(request, call_next):
    return apply_security_headers(await call_next(request), request.url.path)


@app.exception_handler(Exception)
async def unexpected_error(request, _error):
    return apply_security_headers(Response("Internal Server Error", status_code=500, media_type="text/plain"), request.url.path)

if metrics.ENABLED:
    @app.middleware("http")
    async def record_ai_requests(request, call_next):
        path = request.url.path
        submission = request.method == "POST" and (path.startswith("/api/ai/") or path.endswith("/events/suggest"))
        translated_subtitles = path.endswith("/subtitles") and request.query_params.get("target", "original") != "original"
        if not (submission or translated_subtitles):
            return await call_next(request)
        metrics.record("AIHTTPRequests")
        try:
            response = await call_next(request)
        except Exception:
            metrics.record("AIHTTPFailures")
            raise
        if response.status_code == 429:
            metrics.record("AIHTTPRateLimited")
        if response.status_code >= 500:
            metrics.record("AIHTTPFailures")
        return response


@app.get("/health/live")
def liveness():
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    try:
        await asyncio.to_thread(runtime.db.ping)
        if runtime.manager.bus.redis:
            await runtime.manager.bus.redis.ping()
    except Exception:
        raise HTTPException(503, "dependency unavailable")
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/languages")
def languages():
    return LANGUAGES
