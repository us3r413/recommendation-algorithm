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

from src.pipeline import recommend


# ---------------------------------------------------------------------------
# Lifespan: preload graph at startup (skip if using Neptune)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload the graph into memory before accepting requests.

    When USE_NEPTUNE=true, skip local graph build — queries go to Neptune directly.
    """
    import os
    use_neptune = os.environ.get("USE_NEPTUNE", "false").lower() in ("true", "1", "yes")

    if use_neptune:
        print("[API] USE_NEPTUNE=true — skipping local graph preload (using Neptune)")
        # Verify Neptune connection
        try:
            from src.neptune_client import get_traversal
            g = get_traversal()
            count = g.V().count().next()
            print(f"[API] Neptune connected — {count:,} vertices")
        except Exception as e:
            print(f"[API] WARNING: Neptune unavailable ({e}) — graph ranking will fall back to popularity")
    else:
        from src.graph_builder import get_graph
        print("[API] Preloading local graph...")
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
    request_id: str = Field(default="req_0001", description="請求 ID")
    query: str = Field(default="", description="搜尋查詢字串", examples=["台北 前端工程師 35k以上"])
    talent_no: int = Field(default=0, description="用戶 ID（0 = 匿名）", examples=[0, 12345])
    c0: list[str] | None = Field(default=None, alias="location_code", description="城市代碼列表", examples=[["100100", "100200"]])
    d0: list[str] | None = Field(default=None, alias="duty_code", description="職務類別代碼列表", examples=[["140214", "140213"]])

    model_config = {"populate_by_name": True}


class JobResultItem(BaseModel):
    job_id: str
    rank: int


class RecommendResponse(BaseModel):
    request_id: str
    result: list[JobResultItem]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/recommend", response_model=RecommendResponse)
@app.post("/api/v1/jobs/search", response_model=RecommendResponse)
async def recommend_endpoint(req: RecommendRequest):
    """Return top 10 recommended job listings for the given query."""
    results = recommend(
        query=req.query,
        talent_no=req.talent_no,
        c0=req.c0,
        d0=req.d0,
    )

    # Format output: [{"job_id": "123", "rank": 1}, ...]
    result_items = [
        JobResultItem(job_id=str(r.get("職缺編號", "")), rank=i + 1)
        for i, r in enumerate(results)
    ]

    return RecommendResponse(
        request_id=req.request_id,
        result=result_items,
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
