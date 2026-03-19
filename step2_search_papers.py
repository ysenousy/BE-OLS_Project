"""
step2_search_papers.py – Phase 3: Academic Paper Search

Queries three free APIs for each ontology in ontology_metadata.json:
  1. OpenAlex    – broadest coverage, fast (10 req/s polite pool)
  2. Semantic Scholar – strong CS focus, citation data
  3. CrossRef    – DOI resolution only (resolves embedded DOIs from rdfs:seeAlso)

Results are deduplicated by DOI (or title if no DOI) and merged.
Computes per-ontology relevance metrics.

Outputs:
  ontology_papers.csv / .json               – paper results per ontology
  ontology_papers_metrics.csv / .json       – search relevance metrics
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, fields
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional, Sequence, Set
from urllib.parse import quote

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

RESULTS_PER_QUERY = 10
POLITE_EMAIL = "be-ols-research@example.com"  # for OpenAlex/CrossRef polite pool

OUT_DIR = Path(__file__).parent
METADATA_FILE = OUT_DIR / "ontology_metadata.json"

# Semantic Scholar optional API key
SS_API_KEY: Optional[str] = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")

# Delays (seconds)
OPENALEX_DELAY = 0.2         # OpenAlex: ~10 req/s polite
SEMANTIC_SCHOLAR_DELAY = 5.0 # SS free tier: ~1 req/5s safe
CROSSREF_DELAY = 0.5         # CrossRef polite: ~50 req/s

MAX_RETRIES = 4
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,]+", re.IGNORECASE)

# Relevance filtering
MIN_RELEVANCE_SCORE = 0.05   # drop papers scoring below this
TOP_K_PER_ONTOLOGY = 10      # keep at most this many per ontology (after dedup)


# ---------------------------------------------------------------------------
# Silent incremental save (no print)
# ---------------------------------------------------------------------------

def _save_quiet(rows: Sequence, csv_path: Path, json_path: Path) -> None:
    """Overwrite CSV + JSON without printing (used for incremental saves)."""
    if not rows:
        return
    field_names = [f.name for f in fields(rows[0])]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=field_names)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in rows], fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Shared HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None,
         headers: dict | None = None, delay: float = 1.0,
         label: str = "") -> Optional[dict]:
    """GET JSON with retries + exponential backoff on 429."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers or {}, timeout=30)
            if resp.status_code == 429:
                wait = delay * (2 ** (attempt + 2))
                print(f"    [{label}] 429 rate-limited, waiting {wait:.0f}s …")
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay * (2 ** attempt))
            else:
                print(f"    [{label}] Failed after {MAX_RETRIES} attempts: {exc}")
                return None
    return None


# ===================================================================
# 1. OpenAlex  (fastest, broadest)
# ===================================================================

OPENALEX_WORKS = "https://api.openalex.org/works"


def _openalex_headers() -> dict:
    return {"User-Agent": f"BE-OLS-Research/1.0 (mailto:{POLITE_EMAIL})"}


def search_openalex(query: str) -> List[dict]:
    """Search OpenAlex Works for papers matching *query*."""
    params = {
        "search": query,
        "per_page": RESULTS_PER_QUERY,
        "select": "id,doi,title,authorships,publication_year,primary_location,cited_by_count,abstract_inverted_index,open_access,concepts",
    }
    data = _get(OPENALEX_WORKS, params=params,
                headers=_openalex_headers(), delay=OPENALEX_DELAY,
                label="OpenAlex")
    if data and "results" in data:
        return data["results"]
    return []


def _openalex_to_paper(ontology_filename: str, query: str, work: dict) -> PaperResult:
    """Convert an OpenAlex work record to PaperResult."""
    # Reconstruct abstract from inverted index
    abstract = ""
    inv_idx = work.get("abstract_inverted_index") or {}
    if inv_idx:
        word_positions: List[tuple] = []
        for word, positions in inv_idx.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort()
        abstract = " ".join(w for _, w in word_positions)

    authors = ", ".join(
        (a.get("author") or {}).get("display_name", "")
        for a in (work.get("authorships") or [])
    )
    doi_raw = work.get("doi") or ""
    doi = doi_raw.replace("https://doi.org/", "").strip() if doi_raw else ""

    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    venue = source.get("display_name", "") or ""

    oa = work.get("open_access") or {}
    concepts = work.get("concepts") or []
    fos = ", ".join(c.get("display_name", "") for c in concepts[:5])

    return PaperResult(
        ontology_filename=ontology_filename,
        search_query=query,
        source="openalex",
        paper_id=work.get("id", ""),
        title=work.get("title", "") or "",
        authors=authors,
        year=work.get("publication_year") or 0,
        venue=venue,
        citation_count=work.get("cited_by_count") or 0,
        abstract=abstract,
        doi=doi,
        url=work.get("id", ""),
        is_open_access=bool(oa.get("is_oa")),
        fields_of_study=fos,
    )


# ===================================================================
# 2. Semantic Scholar
# ===================================================================

SS_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
SS_PAPER = "https://api.semanticscholar.org/graph/v1/paper"
SS_FIELDS = "title,authors,year,venue,citationCount,abstract,externalIds,url,isOpenAccess,fieldsOfStudy"


def _ss_headers() -> dict:
    h: dict = {"Accept": "application/json"}
    if SS_API_KEY:
        h["x-api-key"] = SS_API_KEY
    return h


def search_semantic_scholar(query: str) -> List[dict]:
    """Search Semantic Scholar relevance API."""
    params = {"query": query, "fields": SS_FIELDS, "limit": RESULTS_PER_QUERY}
    data = _get(SS_SEARCH, params=params,
                headers=_ss_headers(), delay=SEMANTIC_SCHOLAR_DELAY,
                label="SemanticScholar")
    if data and "data" in data:
        return data["data"]
    return []


def lookup_doi_semantic_scholar(doi: str) -> Optional[dict]:
    """Resolve a DOI via Semantic Scholar."""
    url = f"{SS_PAPER}/DOI:{doi}"
    return _get(url, params={"fields": SS_FIELDS},
                headers=_ss_headers(), delay=SEMANTIC_SCHOLAR_DELAY,
                label="SS-DOI")


def _ss_to_paper(ontology_filename: str, query: str, paper: dict,
                 src: str = "semantic_scholar") -> PaperResult:
    authors = ", ".join(
        a.get("name", "") for a in (paper.get("authors") or [])
    )
    ext_ids = paper.get("externalIds") or {}
    fos = paper.get("fieldsOfStudy") or []
    return PaperResult(
        ontology_filename=ontology_filename,
        search_query=query,
        source=src,
        paper_id=paper.get("paperId", ""),
        title=paper.get("title", "") or "",
        authors=authors,
        year=paper.get("year") or 0,
        venue=paper.get("venue", "") or "",
        citation_count=paper.get("citationCount") or 0,
        abstract=paper.get("abstract", "") or "",
        doi=ext_ids.get("DOI", "") or "",
        url=paper.get("url", "") or "",
        is_open_access=bool(paper.get("isOpenAccess")),
        fields_of_study=", ".join(fos),
    )


# ===================================================================
# 3. CrossRef  (DOI resolution + search)
# ===================================================================

CROSSREF_WORKS = "https://api.crossref.org/works"


def _crossref_headers() -> dict:
    return {"User-Agent": f"BE-OLS-Research/1.0 (mailto:{POLITE_EMAIL})"}


def search_crossref(query: str) -> List[dict]:
    """Search CrossRef works by query string."""
    params = {
        "query": query,
        "rows": RESULTS_PER_QUERY,
        "select": "DOI,title,author,published-print,published-online,container-title,is-referenced-by-count,abstract,URL,subject",
    }
    data = _get(CROSSREF_WORKS, params=params,
                headers=_crossref_headers(), delay=CROSSREF_DELAY,
                label="CrossRef")
    if data and "message" in data:
        return (data["message"].get("items") or [])
    return []


def lookup_doi_crossref(doi: str) -> Optional[dict]:
    """Resolve a single DOI via CrossRef."""
    url = f"{CROSSREF_WORKS}/{quote(doi, safe='')}"
    data = _get(url, headers=_crossref_headers(), delay=CROSSREF_DELAY,
                label="CR-DOI")
    if data and "message" in data:
        return data["message"]
    return None


def _crossref_to_paper(ontology_filename: str, query: str,
                       item: dict, src: str = "crossref") -> PaperResult:
    title_list = item.get("title") or []
    title = title_list[0] if title_list else ""

    authors = ", ".join(
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in (item.get("author") or [])
    )

    # Year: prefer print date, fallback to online
    year = 0
    for date_key in ("published-print", "published-online"):
        parts = (item.get(date_key) or {}).get("date-parts") or [[]]
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break

    venue_list = item.get("container-title") or []
    venue = venue_list[0] if venue_list else ""

    abstract_raw = item.get("abstract", "") or ""
    # CrossRef abstracts often have JATS XML tags — strip them
    abstract = re.sub(r"<[^>]+>", "", abstract_raw).strip()

    subjects = item.get("subject") or []

    return PaperResult(
        ontology_filename=ontology_filename,
        search_query=query,
        source=src,
        paper_id=item.get("DOI", ""),
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        citation_count=item.get("is-referenced-by-count") or 0,
        abstract=abstract,
        doi=item.get("DOI", "") or "",
        url=item.get("URL", "") or "",
        is_open_access=False,  # CrossRef doesn't reliably report OA
        fields_of_study=", ".join(subjects[:5]),
    )


# ===================================================================
# Deduplication
# ===================================================================

def _dedup_key(paper: PaperResult) -> str:
    """Return a dedup key: DOI if available, else lowercased title."""
    if paper.doi:
        return paper.doi.lower().strip()
    return paper.title.lower().strip()


def deduplicate_papers(papers: List[PaperResult]) -> List[PaperResult]:
    """Remove duplicate papers, keeping the one with more metadata."""
    seen: Dict[str, PaperResult] = {}
    for p in papers:
        key = _dedup_key(p)
        if not key:
            continue
        if key not in seen:
            seen[key] = p
        else:
            existing = seen[key]
            # Prefer the record with more info (abstract, citation count)
            new_score = bool(p.abstract) + bool(p.citation_count) + bool(p.authors)
            old_score = bool(existing.abstract) + bool(existing.citation_count) + bool(existing.authors)
            if new_score > old_score:
                seen[key] = p
    return list(seen.values())


# ===================================================================
# DOI extraction from rdfs:seeAlso
# ===================================================================

def extract_dois(see_also: str) -> List[str]:
    return DOI_RE.findall(see_also)


# ===================================================================
# Relevance scoring & filtering
# ===================================================================

# Built-environment / ontology domain terms used for domain-relevance boost
_DOMAIN_KEYWORDS = {
    "ontology", "semantic", "linked data", "rdf", "owl", "knowledge graph",
    "building", "construction", "architecture", "bim", "ifc", "hvac",
    "energy", "sensor", "iot", "smart", "infrastructure", "geospatial",
    "gis", "urban", "city", "environment", "sustainability", "indoor",
    "facility", "space", "topology", "interoperability", "metadata",
}


def _compute_relevance(query: str, paper: PaperResult) -> float:
    """Compute a composite relevance score in [0, 1] for a paper.

    Components (weighted sum):
      - title_overlap  (40%): Jaccard similarity between query and title
      - abstract_overlap (30%): Jaccard similarity between query and abstract
      - domain_boost   (20%): fraction of paper text tokens that are domain terms
      - source_boost    (10%): DOI look-ups get full credit (directly cited)
    """
    title_sim = jaccard_similarity(query, paper.title)
    abstract_sim = jaccard_similarity(query, paper.abstract) if paper.abstract else 0.0

    # Domain term presence in title + abstract
    combined = f"{paper.title} {paper.abstract} {paper.fields_of_study}".lower()
    combined_tokens = set(re.findall(r"\w+", combined))
    if combined_tokens:
        domain_hits = sum(1 for kw in _DOMAIN_KEYWORDS if kw in combined)
        domain_score = min(domain_hits / 5.0, 1.0)  # cap at 5 hits → 1.0
    else:
        domain_score = 0.0

    # Source boost: DOI look-ups are directly cited by the ontology
    source_score = 1.0 if paper.source == "doi_lookup" else 0.0

    score = (
        0.40 * title_sim
        + 0.30 * abstract_sim
        + 0.20 * domain_score
        + 0.10 * source_score
    )
    return round(score, 4)


def score_and_filter(query: str, papers: List[PaperResult]) -> List[PaperResult]:
    """Score papers, drop those below threshold, sort by relevance, keep top-K."""
    for p in papers:
        p.relevance_score = _compute_relevance(query, p)

    # Keep DOI look-ups regardless of score (directly referenced by the ontology)
    relevant = [
        p for p in papers
        if p.relevance_score >= MIN_RELEVANCE_SCORE or p.source == "doi_lookup"
    ]

    # Sort descending by relevance, then by citation count as tiebreaker
    relevant.sort(key=lambda p: (p.relevance_score, p.citation_count), reverse=True)

    return relevant[:TOP_K_PER_ONTOLOGY]


# ===================================================================
# Metrics computation
# ===================================================================

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

    title_overlaps = [jaccard_similarity(query, p.title) for p in papers]
    abstract_overlaps = [
        jaccard_similarity(query, p.abstract) for p in papers if p.abstract
    ]
    m.avg_title_keyword_overlap = round(mean(title_overlaps), 4)
    m.max_title_overlap = round(max(title_overlaps), 4)
    if abstract_overlaps:
        m.avg_abstract_keyword_overlap = round(mean(abstract_overlaps), 4)

    m.avg_citation_count = round(mean(p.citation_count for p in papers), 1)

    years = [p.year for p in papers if p.year]
    if years:
        m.median_year = int(median(years))

    cs_eng = sum(
        1 for p in papers
        if any(f in (p.fields_of_study or "") for f in ("Computer Science", "Engineering"))
    )
    m.cs_engineering_ratio = round(cs_eng / len(papers), 4)

    oa = sum(1 for p in papers if p.is_open_access)
    m.open_access_ratio = round(oa / len(papers), 4)

    return m


# ===================================================================
# Summary printer
# ===================================================================

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


# ===================================================================
# Main
# ===================================================================

def main() -> None:
    if not METADATA_FILE.exists():
        print(f"ERROR: {METADATA_FILE} not found. Run step1_extract_metadata.py first.")
        sys.exit(1)

    with open(METADATA_FILE, "r", encoding="utf-8") as fh:
        ontologies = json.load(fh)

    print(f"Loaded {len(ontologies)} ontologies from {METADATA_FILE.name}")
    print("APIs: OpenAlex + Semantic Scholar (search), CrossRef (DOI resolution)\n")

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

        primary_query = query.split(" | ")[0].strip()
        print(f"[{i}/{len(ontologies)}] {filename} — \"{primary_query}\"")

        papers: List[PaperResult] = []

        # --- 1. OpenAlex (fast, broad) ---
        time.sleep(OPENALEX_DELAY)
        oa_results = search_openalex(primary_query)
        oa_papers = [_openalex_to_paper(filename, primary_query, w) for w in oa_results]
        papers.extend(oa_papers)
        print(f"    OpenAlex: {len(oa_papers)} results")

        # --- 2. Semantic Scholar ---
        time.sleep(SEMANTIC_SCHOLAR_DELAY)
        ss_results = search_semantic_scholar(primary_query)
        ss_papers = [_ss_to_paper(filename, primary_query, p) for p in ss_results]
        papers.extend(ss_papers)
        print(f"    Semantic Scholar: {len(ss_papers)} results")

        # --- 3. DOI look-ups from rdfs:seeAlso (CrossRef + SS fallback) ---
        doi_hit = False
        dois = extract_dois(see_also)
        seen_dois: Set[str] = {p.doi.lower() for p in papers if p.doi}
        for doi in dois:
            if doi.lower() in seen_dois:
                doi_hit = True
                continue
            # Try CrossRef first (faster), then Semantic Scholar
            time.sleep(CROSSREF_DELAY)
            cr_data = lookup_doi_crossref(doi)
            if cr_data:
                doi_hit = True
                papers.append(_crossref_to_paper(filename, f"DOI:{doi}", cr_data, src="doi_lookup"))
            else:
                time.sleep(SEMANTIC_SCHOLAR_DELAY)
                ss_data = lookup_doi_semantic_scholar(doi)
                if ss_data:
                    doi_hit = True
                    papers.append(_ss_to_paper(filename, f"DOI:{doi}", ss_data, src="doi_lookup"))

        # --- 4. Deduplicate ---
        papers = deduplicate_papers(papers)

        # --- 5. Score & filter by relevance ---
        before_filter = len(papers)
        papers = score_and_filter(primary_query, papers)
        all_papers.extend(papers)

        # --- 6. Metrics ---
        met = compute_metrics(filename, primary_query, papers, doi_hit)
        all_metrics.append(met)
        print(f"    → {before_filter} unique → {len(papers)} relevant (top-{TOP_K_PER_ONTOLOGY}, ≥{MIN_RELEVANCE_SCORE}), DOI hit: {doi_hit}")
        if papers:
            print(f"      best: [{papers[0].relevance_score:.3f}] {papers[0].title[:70]}")

        # --- 7. Incremental save (overwrite after each ontology) ---
        _save_quiet(all_papers, OUT_DIR / "ontology_papers.csv", OUT_DIR / "ontology_papers.json")
        _save_quiet(all_metrics, OUT_DIR / "ontology_papers_metrics.csv", OUT_DIR / "ontology_papers_metrics.json")

    # Final summary
    print(f"\nTotal: {len(all_papers)} paper results across {len(all_metrics)} ontologies")
    print_metrics_summary(all_metrics)

    print("Done ✓")


if __name__ == "__main__":
    main()
