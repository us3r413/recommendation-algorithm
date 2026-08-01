"""
skill_extractor.py — Hybrid skill extraction for the ability knowledge graph.

Two extraction modes:
1. Structured: Parse comma-separated values from 電腦技能資料, 工作技能, 專業證照
2. LLM: Use Bedrock Claude to extract skills from 職務名稱 + 職務內容

The hybrid strategy:
  - If any of the 3 structured fields is non-empty → use structured (confidence=1.0)
  - Otherwise → call LLM on 職務名稱 + 職務內容 (confidence=0.8)

Extracted skills are cached to CSV (dataset/job_skills_cache.csv) to avoid
repeated LLM calls across runs.

Usage:
    python -m src.skill_extractor          # Extract & cache all skills
    python -m src.skill_extractor --llm    # Include LLM extraction for missing
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JOBS_PATH = "dataset/職缺.csv"
EVENTS_PATH = "dataset/userBehaviorEvents.csv"
SKILLS_CACHE_PATH = "dataset/job_skills_cache.csv"

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

# Max concurrent LLM calls
MAX_WORKERS = 5
# Max content length sent to LLM
MAX_CONTENT_LENGTH = 500

# ---------------------------------------------------------------------------
# LLM Prompt
# ---------------------------------------------------------------------------

SKILL_EXTRACTION_PROMPT = """你是職缺技能萃取器。根據以下職缺標題和內容，提取最多5個核心技能/能力標籤。

職缺標題：{title}
職缺內容：{content}

回傳格式（JSON array）：["技能1", "技能2", ...]

規則：
- 只輸出具體、可量化的技能（如 Python、專案管理、會計、Excel）
- 不要輸出軟實力（如 團隊合作、溝通能力、細心）
- 不要輸出職稱本身
- 如果無法判斷具體技能，回傳空陣列 []"""


# ---------------------------------------------------------------------------
# Structured extraction
# ---------------------------------------------------------------------------


def extract_structured_skills(row: pd.Series) -> list[str]:
    """Extract skills from the 3 structured columns of a job listing.

    Parses comma-separated values from:
      - 電腦技能資料 (computer skills)
      - 工作技能 (work skills)
      - 專業證照 (certifications)

    Returns a deduplicated list of skill strings, or empty list if all are null.
    """
    skills = set()

    for col in ("電腦技能資料", "工作技能", "專業證照"):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            # Split by comma and clean up
            for skill in str(val).split(","):
                skill = skill.strip()
                if skill:
                    skills.add(skill)

    return sorted(skills)


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_bedrock_client = None


def _get_bedrock_client():
    """Lazy-init Bedrock runtime client."""
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_client


def extract_skills_llm(title: str, content: str) -> list[str]:
    """Extract skills from job title + content using Bedrock Claude.

    Args:
        title: Job title (職務名稱).
        content: Job description (職務內容), truncated to MAX_CONTENT_LENGTH.

    Returns:
        List of up to 5 skill strings, or empty list on failure.
    """
    content_truncated = (content or "")[:MAX_CONTENT_LENGTH]
    if not title and not content_truncated:
        return []

    prompt = SKILL_EXTRACTION_PROMPT.format(title=title, content=content_truncated)

    client = _get_bedrock_client()

    try:
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 200,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        result = json.loads(response["body"].read())
        text = result["content"][0]["text"].strip()

        # Parse JSON array from response
        # Handle cases where LLM wraps in markdown code block
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        skills = json.loads(text)
        if isinstance(skills, list):
            return [str(s).strip() for s in skills if str(s).strip()][:5]
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Batch extraction pipeline
# ---------------------------------------------------------------------------


def extract_all_skills(include_llm: bool = False, limit_llm: int = 0) -> pd.DataFrame:
    """Run the full hybrid extraction pipeline.

    1. Load jobs CSV (only jobs with interactions, from userBehaviorEvents)
    2. For each job, try structured extraction first
    3. If include_llm=True, use LLM for jobs with no structured skills
    4. Save results to SKILLS_CACHE_PATH

    Args:
        include_llm: Whether to run LLM extraction for jobs missing structured skills.
        limit_llm: Max number of LLM calls (0=unlimited). For cost control during dev.

    Returns:
        DataFrame with columns: [job_id, skills, source]
        - skills: pipe-separated skill list (e.g. "Python|Excel|SQL")
        - source: "structured" or "llm"
    """
    print("[SkillExtractor] Loading job data...")
    jobs = pd.read_csv(
        JOBS_PATH,
        usecols=["職缺編號", "職務名稱", "職務內容", "電腦技能資料", "工作技能", "專業證照"],
    )

    # Only extract for jobs that have user interactions
    print("[SkillExtractor] Filtering to interacted jobs...")
    events = pd.read_csv(EVENTS_PATH, usecols=["job_id"])
    interacted_ids = set(events["job_id"].dropna().astype(int).unique())
    jobs = jobs[jobs["職缺編號"].isin(interacted_ids)].copy()
    print(f"  {len(jobs):,} jobs with interactions")

    # Load existing cache if available (incremental extraction)
    cached_ids: set[int] = set()
    results: list[dict] = []
    if os.path.exists(SKILLS_CACHE_PATH):
        print("[SkillExtractor] Loading existing cache...")
        cache_df = pd.read_csv(SKILLS_CACHE_PATH)
        cached_ids = set(cache_df["job_id"].astype(int).unique())
        results = cache_df.to_dict("records")
        print(f"  {len(cached_ids):,} jobs already cached")

    # Filter out already-cached jobs
    jobs = jobs[~jobs["職缺編號"].isin(cached_ids)]
    print(f"  {len(jobs):,} new jobs to process")

    if jobs.empty:
        print("[SkillExtractor] Nothing to do — all jobs cached")
        return pd.DataFrame(results)

    # Phase 1: Structured extraction
    print("[SkillExtractor] Phase 1: Structured extraction...")
    needs_llm = []
    structured_count = 0

    for _, row in jobs.iterrows():
        job_id_val = int(row["職缺編號"])
        skills = extract_structured_skills(row)
        if skills:
            results.append({
                "job_id": job_id_val,
                "skills": "|".join(skills),
                "source": "structured",
            })
            structured_count += 1
        else:
            needs_llm.append(row)

    print(f"  Structured: {structured_count:,} jobs")
    print(f"  Needs LLM: {len(needs_llm):,} jobs")

    # Phase 2: LLM extraction (optional)
    llm_count = 0
    if include_llm and needs_llm:
        llm_batch = needs_llm
        if limit_llm > 0:
            llm_batch = llm_batch[:limit_llm]

        print(f"[SkillExtractor] Phase 2: LLM extraction ({len(llm_batch):,} jobs)...")

        def _extract_one(row):
            job_id_val = int(row["職缺編號"])
            title = str(row["職務名稱"]) if pd.notna(row["職務名稱"]) else ""
            content = str(row["職務內容"]) if pd.notna(row["職務內容"]) else ""
            skills = extract_skills_llm(title, content)
            return {"job_id": job_id_val, "skills": "|".join(skills), "source": "llm"}

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_extract_one, row): i for i, row in enumerate(llm_batch)}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                llm_count += 1
                if llm_count % 100 == 0:
                    print(f"    LLM progress: {llm_count}/{len(llm_batch)}")

        print(f"  LLM extracted: {llm_count:,} jobs")

    # Save cache
    print(f"[SkillExtractor] Saving cache to {SKILLS_CACHE_PATH}...")
    df = pd.DataFrame(results)
    df.to_csv(SKILLS_CACHE_PATH, index=False)
    print(f"  Total cached: {len(df):,} jobs")

    return df


def load_skills_cache() -> pd.DataFrame:
    """Load the skills cache from disk.

    Returns DataFrame with columns: [job_id, skills, source]
    Returns empty DataFrame if cache doesn't exist.
    """
    if os.path.exists(SKILLS_CACHE_PATH):
        return pd.read_csv(SKILLS_CACHE_PATH)
    return pd.DataFrame(columns=["job_id", "skills", "source"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    include_llm = "--llm" in sys.argv
    limit_arg = 0
    for arg in sys.argv:
        if arg.startswith("--limit="):
            limit_arg = int(arg.split("=")[1])

    start = time.perf_counter()
    df = extract_all_skills(include_llm=include_llm, limit_llm=limit_arg)
    elapsed = time.perf_counter() - start

    print(f"\n[SkillExtractor] Done in {elapsed:.1f}s")
    print(f"  Total jobs with skills: {len(df):,}")
    if not df.empty:
        print(f"  Structured: {(df['source'] == 'structured').sum():,}")
        print(f"  LLM: {(df['source'] == 'llm').sum():,}")
        # Show top skills
        all_skills = df["skills"].str.split("|").explode()
        print(f"  Unique skills: {all_skills.nunique():,}")
        print(f"  Top 10 skills:")
        for skill, count in all_skills.value_counts().head(10).items():
            print(f"    {skill}: {count}")
