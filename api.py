"""
api.py — FastAPI endpoint for the job recommendation pipeline.

Startup:
    uvicorn api:app --host 0.0.0.0 --port 8000

Endpoints:
    POST /recommend  — main recommendation endpoint
    GET  /health     — health check
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from src.graph_builder import get_graph
from src.pipeline import recommend


# ---------------------------------------------------------------------------
# Lifespan: preload graph at startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload the graph into memory before accepting requests."""
    print("[API] Preloading graph...")
    start = time.perf_counter()
    get_graph()
    elapsed = time.perf_counter() - start
    print(f"[API] Graph ready in {elapsed:.1f}s")
    yield


app = FastAPI(
    title="職缺推薦 API",
    description="根據搜尋查詢推薦 Top 10 職缺",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RecommendRequest(BaseModel):
    query: str = Field(default="", description="搜尋查詢字串", examples=["台北 前端工程師 35k以上"])
    talent_no: int = Field(default=0, description="用戶 ID（0 = 匿名）", examples=[0, 12345])
    c0: list[str] | None = Field(default=None, description="城市代碼列表", examples=[["100100", "100200"]])
    d0: list[str] | None = Field(default=None, description="職務類別代碼列表", examples=[["140214", "140213"]])


class JobResult(BaseModel):
    職缺編號: int | None = None
    職務名稱: str | None = None
    工作城市: str | None = None
    薪資下限: float | None = None
    薪資上限: float | None = None
    職務大類: str | None = None
    職務中類: str | None = None
    職務小類: str | None = None
    職缺屬性: str | None = None


class RecommendResponse(BaseModel):
    results: list[dict]
    count: int
    elapsed_ms: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/recommend", response_model=RecommendResponse)
async def recommend_endpoint(req: RecommendRequest):
    """Return top 10 recommended job listings for the given query."""
    start = time.perf_counter()

    results = recommend(
        query=req.query,
        talent_no=req.talent_no,
        c0=req.c0,
        d0=req.d0,
    )

    elapsed_ms = (time.perf_counter() - start) * 1000

    return RecommendResponse(
        results=results,
        count=len(results),
        elapsed_ms=round(elapsed_ms, 1),
    )


@app.get("/recommend", response_model=RecommendResponse)
async def recommend_get(
    query: str = Query(default="", description="搜尋查詢字串"),
    talent_no: int = Query(default=0, description="用戶 ID（0 = 匿名）"),
    c0: list[str] | None = Query(default=None, description="城市代碼列表"),
    d0: list[str] | None = Query(default=None, description="職務類別代碼列表"),
):
    """GET version of /recommend for easy browser/curl testing."""
    start = time.perf_counter()

    results = recommend(
        query=query,
        talent_no=talent_no,
        c0=c0,
        d0=d0,
    )

    elapsed_ms = (time.perf_counter() - start) * 1000

    return RecommendResponse(
        results=results,
        count=len(results),
        elapsed_ms=round(elapsed_ms, 1),
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
