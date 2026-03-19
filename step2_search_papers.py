"""
step2_search_papers.py – Phase 3: Academic Paper Search via Semantic Scholar

For each ontology in ontology_metadata.json, queries the Semantic Scholar API
for related papers using the derived search keywords.  Also looks up any DOIs
embedded in rdfs:seeAlso.  Computes per-ontology relevance metrics.

Outputs:
  ontology_papers.csv / .json               – paper results per ontology
  ontology_papers_metrics.csv / .json       – search relevance metrics
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from statistics import mean, median
from typing import List, Optional

import requests

from common import (
    PaperResult,
    PaperSearchMetrics,
    jaccard_similarity,
    save_csv,
    save_json,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEMANTIC_SCHOLAR_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
SEMANTIC_SCHOLAR_PAPER = "https://api.semanticscholar.org/graph/v1/paper"
FIELDS = "title,authors,year,venue,citationCount,abstract,externalIds,url,isOpenAccess,fieldsOfStudy"
RESULTS_PER_QUERY = 10
REQUEST_DELAY = 4.0          # seconds between requests (free-tier safe)
MAX_RETRIES = 4

OUT_DIR = Path(__file__).parent
METADATA_FILE = OUT_DIR / "ontology_metadata.json"

# Optional API key via environment variable for higher rate limits
API_KEY: Optional[str] = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")


# ---------------------------------------------------------------------------
# HTTP helpers with retry
# ---------------------------------------------------------------------------

def _headers() -> dict:
    h = {"Accept": "application/json"}
    if API_KEY:
        h["x-api-key"] = API_KEY
    return h


def _get_json(url: str, params: dict | None = None) -> Optional[dict]:
    """GET with exponential back-off retry. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=30)
            if resp.status_code == 429:
                wait = REQUEST_DELAY * (2 ** attempt)
                print(f"    Rate-limited, waiting {wait:.0f}s …")
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(REQUEST_DELAY * (2 ** attempt))
            else:
                print(f"    Request failed after {MAX_RETRIES} attempts: {exc}")
                return None
    return None


# ---------------------------------------------------------------------------
# Step 8 – Relevance search
# ---------------------------------------------------------------------------

def search_papers(query: str) -> List[dict]:
    """Search Semantic Scholar for up to RESULTS_PER_QUERY papers."""
    params = {
        "query": query,
        "fields": FIELDS,
        "limit": RESULTS_PER_QUERY,
    }
    data = _get_json(SEMANTIC_SCHOLAR_SEARCH, params=params)
    if data and "data" in data:
        return data["data"]
    return []


# ---------------------------------------------------------------------------
# Step 9 – DOI look-up
# ---------------------------------------------------------------------------

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,]+", re.IGNORECASE)


def extract_dois(see_also: str) -> List[str]:
    """Extract DOI strings from a comma-separated list of URIs."""
    return _DOI_RE.findall(see_also)


def lookup_paper_by_doi(doi: str) -> Optional[dict]:
    """Fetch a single paper record by DOI."""
    url = f"{SEMANTIC_SCHOLAR_PAPER}/DOI:{doi}"
    return _get_json(url, params={"fields": FIELDS})


# ---------------------------------------------------------------------------
# Convert API response → PaperResult
# ---------------------------------------------------------------------------

def _to_paper_result(ontology_filename: str, query: str, paper: dict) -> PaperResult:
    authors_str = ", ".join(
        a.get("name", "") for a in (paper.get("authors") or [])
    )
    ext_ids = paper.get("externalIds") or {}
    fos = paper.get("fieldsOfStudy") or []
    return PaperResult(
        ontology_filename=ontology_filename,
        search_query=query,
        paper_id=paper.get("paperId", ""),
        title=paper.get("title", "") or "",
        authors=authors_str,
        year=paper.get("year") or 0,
        venue=paper.get("venue", "") or "",
        citation_count=paper.get("citationCount") or 0,
        abstract=paper.get("abstract", "") or "",
        doi=ext_ids.get("DOI", "") or "",
        url=paper.get("url", "") or "",
        is_open_access=bool(paper.get("isOpenAccess")),
        fields_of_study=", ".join(fos),
    )


# ---------------------------------------------------------------------------
# Step 11b – Compute metrics for one ontology
# ---------------------------------------------------------------------------

def compute_metrics(
    ontology_filename: str,
    query: str,
    papers: List[PaperResult],
    doi_hit: bool,
) -> PaperSearchMetrics:
    m = PaperSearchMetrics(
        ontology_filename=ontology_filename,
        search_query=query,
        results_returned=len(papers),
        doi_direct_hit=doi_hit,
    )
    if not papers:
        return m

    # Jaccard overlaps
    title_overlaps = [jaccard_similarity(query, p.title) for p in papers]
    abstract_overlaps = [
        jaccard_similarity(query, p.abstract) for p in papers if p.abstract
    ]
    m.avg_title_keyword_overlap = round(mean(title_overlaps), 4)
    m.max_title_overlap = round(max(title_overlaps), 4)
    if abstract_overlaps:
        m.avg_abstract_keyword_overlap = round(mean(abstract_overlaps), 4)

    # Citation stats
    m.avg_citation_count = round(mean(p.citation_count for p in papers), 1)

    # Median year
    years = [p.year for p in papers if p.year]
    if years:
        m.median_year = int(median(years))

    # Field-of-study ratio
    cs_eng = sum(
        1 for p in papers
        if any(f in (p.fields_of_study or "") for f in ("Computer Science", "Engineering"))
    )
    m.cs_engineering_ratio = round(cs_eng / len(papers), 4)

    # Open access ratio
    oa = sum(1 for p in papers if p.is_open_access)
    m.open_access_ratio = round(oa / len(papers), 4)

    return m


# ---------------------------------------------------------------------------
# Step 11b – Print summary
# ---------------------------------------------------------------------------

def print_metrics_summary(metrics_list: List[PaperSearchMetrics]) -> None:
    total = len(metrics_list)
    with_results = [m for m in metrics_list if m.results_returned > 0]
    without = [m for m in metrics_list if m.results_returned == 0]

    print("\n" + "=" * 60)
    print("  PAPER SEARCH EVALUATION SUMMARY")
    print("=" * 60)
    print(f"  Total ontologies searched : {total}")
    print(f"  With ≥1 paper result      : {len(with_results)}")
    print(f"  With 0 results            : {len(without)}")

    if with_results:
        avg_res = mean(m.results_returned for m in with_results)
        print(f"  Mean results/ontology     : {avg_res:.1f}")
        avg_t = mean(m.avg_title_keyword_overlap for m in with_results)
        avg_a = mean(
            m.avg_abstract_keyword_overlap for m in with_results
            if m.avg_abstract_keyword_overlap > 0
        ) if any(m.avg_abstract_keyword_overlap > 0 for m in with_results) else 0.0
        print(f"  Mean title overlap        : {avg_t:.4f}")
        print(f"  Mean abstract overlap     : {avg_a:.4f}")
        avg_cit = mean(m.avg_citation_count for m in with_results)
        print(f"  Mean citation count       : {avg_cit:.1f}")
        years = [m.median_year for m in with_results if m.median_year]
        if years:
            print(f"  Median year (across all)  : {int(median(years))}")
        doi_hits = sum(1 for m in metrics_list if m.doi_direct_hit)
        print(f"  Ontologies with DOI hit   : {doi_hits}/{total}")

    if without:
        print(f"\n  Ontologies with 0 results ({len(without)}):")
        for m in without[:10]:
            print(f"    - {m.ontology_filename}")
        if len(without) > 10:
            print(f"    … and {len(without) - 10} more")

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Load ontology metadata from step 1
    if not METADATA_FILE.exists():
        print(f"ERROR: {METADATA_FILE} not found. Run step1_extract_metadata.py first.")
        sys.exit(1)

    with open(METADATA_FILE, "r", encoding="utf-8") as fh:
        ontologies = json.load(fh)

    print(f"Loaded {len(ontologies)} ontologies from {METADATA_FILE.name}")

    all_papers: List[PaperResult] = []
    all_metrics: List[PaperSearchMetrics] = []

    for i, ont in enumerate(ontologies, 1):
        filename = ont["filename"]
        query = ont["search_keywords"]
        see_also = ont.get("see_also", "")

        if not query:
            print(f"[{i}/{len(ontologies)}] {filename} — no search keywords, skipping")
            all_metrics.append(PaperSearchMetrics(ontology_filename=filename))
            continue

        # Use first component of search_keywords (before " | ") as the query
        primary_query = query.split(" | ")[0].strip()
        print(f"[{i}/{len(ontologies)}] {filename} — query: \"{primary_query}\"")

        # Step 8 — Relevance search
        time.sleep(REQUEST_DELAY)
        raw_papers = search_papers(primary_query)
        papers = [_to_paper_result(filename, primary_query, p) for p in raw_papers]

        # Step 9 — DOI look-ups
        doi_hit = False
        dois = extract_dois(see_also)
        seen_ids = {p.paper_id for p in papers}
        for doi in dois:
            time.sleep(REQUEST_DELAY)
            paper_data = lookup_paper_by_doi(doi)
            if paper_data:
                doi_hit = True
                pid = paper_data.get("paperId", "")
                if pid and pid not in seen_ids:
                    papers.append(_to_paper_result(filename, f"DOI:{doi}", paper_data))
                    seen_ids.add(pid)

        all_papers.extend(papers)

        # Step 11b — Metrics
        met = compute_metrics(filename, primary_query, papers, doi_hit)
        all_metrics.append(met)
        print(f"  → {met.results_returned} papers, title overlap {met.avg_title_keyword_overlap:.3f}, DOI hit: {doi_hit}")

    # Step 11 — Save results
    print(f"\n[Step 11] Saving {len(all_papers)} paper results …")
    save_csv(all_papers, OUT_DIR / "ontology_papers.csv")
    save_json(all_papers, OUT_DIR / "ontology_papers.json")

    # Step 11b — Save & print metrics
    print("[Step 11b] Saving paper search metrics …")
    save_csv(all_metrics, OUT_DIR / "ontology_papers_metrics.csv")
    save_json(all_metrics, OUT_DIR / "ontology_papers_metrics.json")
    print_metrics_summary(all_metrics)

    print("Done ✓")


if __name__ == "__main__":
    main()
