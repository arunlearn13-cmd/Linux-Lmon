import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .sampler import Sampler

TOKEN = os.getenv("MONITOR_TOKEN")  # optional bearer token for /api/metrics
sampler = Sampler()


@asynccontextmanager
async def lifespan(_: FastAPI):
    sampler.start()
    yield
    sampler.stop()


app = FastAPI(title="Linux Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)


def auth(request: Request):
    if TOKEN and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {TOKEN}"):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/api/metrics", dependencies=[Depends(auth)])
def metrics():
    return JSONResponse(sampler.snapshot, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
