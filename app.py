"""
app.py — FastAPI application for the job recommendation API.

Endpoints:
  POST /recommend  — returns top 10 job recommendations
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


class RecommendRequest(BaseModel):
    query: str = Field(default="", description="Free-text search query (e.g. '台北 前端工程師 35k以上')")
    talent_no: int = Field(default=0, description="User ID. 0 = anonymous, non-zero = signed-in user.")
    c0: list[str] | None = Field(default=None, description="City code filter (e.g. ['100100'])")
    d0: list[str] | None = Field(default=None, description="Job category code filter (e.g. ['140214'])")


class JobResult(BaseModel):
    model_config = {"extra": "allow"}


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


@app.post("/recommend", response_model=RecommendResponse)
async def recommend_endpoint(req: RecommendRequest):
    """Return top 10 job recommendations for the given query and user."""
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
