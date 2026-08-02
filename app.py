"""
app.py — FastAPI application for the job recommendation API.

Endpoints:
  POST /api/v1/jobs/search  — official competition endpoint
  POST /recommend           — detailed results (internal/debug)
  GET  /health              — health check
"""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.pipeline import recommend
from src.ranker import USE_GRAPH_RAG, GRAPH_FOR_ANONYMOUS


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload heavy resources at startup."""
    if USE_GRAPH_RAG or GRAPH_FOR_ANONYMOUS:
        from src.graph_builder import get_graph
        get_graph()
    yield


app = FastAPI(
    title="Job Recommendation API",
    description="Returns top 10 job recommendations based on user query and preferences.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class JobSearchRequest(BaseModel):
    """Official competition request format."""
    query: str = Field(description="搜尋關鍵字（必填）(e.g. '後端工程師')")
    location_code: list[str] | None = Field(default=None, description="地區代碼陣列（選填）(e.g. ['100100'])")
    duty_code: list[str] | None = Field(default=None, description="職務代碼陣列（選填）(e.g. ['140200'])")


class JobSearchResultItem(BaseModel):
    job_id: str = Field(description="職缺編號")
    rank: int = Field(description="排名，從 1 開始，越小越前")


class JobSearchResponse(BaseModel):
    request_id: str = Field(description="追蹤 ID")
    result: list[JobSearchResultItem] = Field(description="排序後的職缺清單")


class RecommendRequest(BaseModel):
    query: str = Field(default="", description="Free-text search query")
    talent_no: int = Field(default=0, description="User ID. 0 = anonymous.")
    c0: list[str] | None = Field(default=None, description="City code filter")
    d0: list[str] | None = Field(default=None, description="Job category code filter")


class RecommendResponse(BaseModel):
    results: list[dict]
    count: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/debug/db-status")
async def db_status():
    """Debug: check if DuckDB tables are loaded and have rows."""
    from src.retriever import _get_db
    con = _get_db()
    jobs_count = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pop_count = con.execute("SELECT COUNT(*) FROM popularity").fetchone()[0]
    sample = con.execute("SELECT 職缺編號, 職務名稱, 工作城市 FROM jobs LIMIT 3").fetchall()
    return {
        "jobs_rows": jobs_count,
        "popularity_rows": pop_count,
        "sample_jobs": [{"id": r[0], "title": r[1], "city": r[2]} for r in sample],
    }


@app.post("/api/v1/jobs/search", response_model=JobSearchResponse)
async def job_search(req: JobSearchRequest):
    """Official competition search endpoint.

    Input:
      - query (required): search keyword
      - location_code (optional): list of city codes
      - duty_code (optional): list of job category codes

    Output:
      - request_id: tracking ID
      - result: list of {job_id, rank} ordered by relevance
    """
    try:
        results = recommend(
            query=req.query,
            talent_no=0,
            c0=req.location_code,
            d0=req.duty_code,
        )

        result_items = [
            JobSearchResultItem(
                job_id=str(int(job["職缺編號"])),
                rank=i,
            )
            for i, job in enumerate(results, 1)
        ]

        return JobSearchResponse(
            request_id=f"req_{uuid.uuid4().hex[:8]}",
            result=result_items,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/recommend", response_model=RecommendResponse)
async def recommend_endpoint(req: RecommendRequest):
    """Return top 10 job recommendations (detailed format, for debugging)."""
    try:
        results = recommend(
            query=req.query,
            talent_no=req.talent_no,
            c0=req.c0,
            d0=req.d0,
        )
        return RecommendResponse(results=results, count=len(results))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
