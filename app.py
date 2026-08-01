"""
app.py — FastAPI application for the job recommendation API.

Endpoints:
  POST /search     — competition-format endpoint (ks/c0/d0 → rank + empStr)
  POST /recommend  — detailed results endpoint (internal/debug)
  GET  /health     — health check
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.pipeline import recommend
from src.ranker import USE_GRAPH_RAG, GRAPH_FOR_ANONYMOUS


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload heavy resources at startup."""
    # Preload graph only if enabled
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


class SearchRequest(BaseModel):
    """Competition-format request matching userSearchLog schema."""
    ks: str = Field(default="", description="搜尋關鍵字 (e.g. '前端工程師')")
    c0: str = Field(default="", description="地區代碼，逗號分隔 (e.g. '100100,100200')")
    d0: str = Field(default="", description="職務代碼，逗號分隔 (e.g. '140214,140213')")
    talentNo: int = Field(default=0, description="求職者編號，0=匿名")


class SearchResponse(BaseModel):
    """Competition-format response with rank and empStr."""
    rank: list[int] = Field(description="排名序號 (1-based)")
    empStr: str = Field(description="排序後之職缺編號清單，逗號分隔")


class RecommendRequest(BaseModel):
    query: str = Field(default="", description="Free-text search query (e.g. '台北 前端工程師 35k以上')")
    talent_no: int = Field(default=0, description="User ID. 0 = anonymous, non-zero = signed-in user.")
    c0: list[str] | None = Field(default=None, description="City code filter (e.g. ['100100'])")
    d0: list[str] | None = Field(default=None, description="Job category code filter (e.g. ['140214'])")


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


@app.post("/search", response_model=SearchResponse)
async def search_endpoint(req: SearchRequest):
    """Competition-format search endpoint.

    Input: ks (keyword), c0 (city codes, comma-separated), d0 (job codes, comma-separated)
    Output: rank (1-based list) + empStr (comma-separated job IDs ordered by relevance)
    """
    try:
        # Parse comma-separated codes into lists (matching userSearchLog format)
        c0_list = [c.strip() for c in req.c0.split(",") if c.strip()] or None
        d0_list = [d.strip() for d in req.d0.split(",") if d.strip()] or None

        results = recommend(
            query=req.ks,
            talent_no=req.talentNo,
            c0=c0_list,
            d0=d0_list,
        )

        # Extract job IDs in ranked order
        job_ids = [str(int(job["職缺編號"])) for job in results]
        rank = list(range(1, len(job_ids) + 1))
        emp_str = ",".join(job_ids)

        return SearchResponse(rank=rank, empStr=emp_str)
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
