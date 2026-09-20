import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .bot import run_forever
from .config import Settings
from .store import Store

settings = Settings()
store = Store(settings.database_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(run_forever(settings, store, False))
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Weather Bot Data", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "running", "mode": settings.mode, "updated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/dashboard")
def dashboard():
    return {**store.dashboard(), "mode": settings.mode, "updated_at": datetime.now(timezone.utc).isoformat()}
