"""기능별 라우터와 정적 화면을 조립하는 FastAPI 애플리케이션."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ai.routes import router as ai_router
from app.auth.routes import router as auth_router
from app.chat.routes import router as chat_router
from app.core import runtime
from app.core.config import FRONTEND_DIR, LANGUAGES, RUN_MEDIA_WORKER
from app.media.routes import router as media_router
from app.rooms.routes import router as rooms_router
from app.users.routes import router as users_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    runtime.loop = asyncio.get_running_loop()
    await runtime.manager.start()
    if RUN_MEDIA_WORKER:
        runtime.media_svc.queue.start()
    try:
        yield
    finally:
        if RUN_MEDIA_WORKER:
            await asyncio.to_thread(runtime.media_svc.queue.stop)
        await runtime.manager.stop()
        runtime.loop = None


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
app.include_router(auth_router)
app.include_router(rooms_router)
app.include_router(media_router)
app.include_router(chat_router)
app.include_router(users_router)
app.include_router(ai_router)


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
