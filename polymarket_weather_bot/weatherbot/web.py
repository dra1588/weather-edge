import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Polymarket Weather Bot</title><style>body{font:14px system-ui;background:#08101d;color:#e8eef8;margin:0;padding:24px}.wrap{max-width:1200px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:20px 0}.card{background:#111b2d;border:1px solid #24324a;border-radius:12px;padding:16px}.k{color:#94a3b8;font-size:12px}.v{font-size:22px;font-weight:700;margin-top:6px}pre{background:#111b2d;padding:16px;border-radius:12px;white-space:pre-wrap;font-size:11px}</style></head><body><div class="wrap"><h1>🌦️ Polymarket Weather Bot</h1><p>Readable view of the existing weather-market research feed.</p><div class="grid"><div class="card"><div class="k">Status</div><div class="v" id="status">Loading…</div></div><div class="card"><div class="k">Mode</div><div class="v" id="mode">—</div></div><div class="card"><div class="k">Open positions</div><div class="v" id="positions">—</div></div><div class="card"><div class="k">Opportunities</div><div class="v" id="opps">—</div></div></div><h2>Dashboard data</h2><pre id="data">Loading…</pre></div><script>async function go(){try{let d=await(await fetch('/api/dashboard')).json();status.textContent='Running';mode.textContent=d.mode||'—';let p=d.open_positions||d.positions||[];let o=d.opportunities||d.markets||d.candidates||[];positions.textContent=Array.isArray(p)?p.length:(p??'—');opps.textContent=Array.isArray(o)?o.length:(o??'—');data.textContent=JSON.stringify(d,null,2)}catch(e){status.textContent='Unavailable'}}go();setInterval(go,10000)</script></body></html>"""
