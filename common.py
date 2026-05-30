"""
common.py – Shared dataclasses and utilities for the BE-OLS pipeline.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import List, Optional, Sequence


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe(val: Optional[str]) -> str:
    """Return *val* stripped, or empty string if None/blank."""
    return (val or "").strip()


def jaccard_similarity(text_a: str, text_b: str) -> float:
    """Token-level Jaccard similarity between two strings (case-insensitive).

    J(A, B) = |tokens(A) ∩ tokens(B)| / |tokens(A) ∪ tokens(B)|
    Returns 0.0 when either string is empty.
    """
    tokens_a = set(re.findall(r"\w+", text_a.lower()))
    tokens_b = set(re.findall(r"\w+", text_b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ---------------------------------------------------------------------------
# Phase 1 – Core data models
# ---------------------------------------------------------------------------

@dataclass
class OntologyRow:
    """Metadata extracted from a single TTL ontology file."""
    filename: str = ""
    title: str = ""
    prefix: str = ""
    namespace_uri: str = ""
    description: str = ""
    version: str = ""
    license: str = ""
    creators: str = ""
    date_modified: str = ""
    date_issued: str = ""
    see_also: str = ""
    imports: str = ""
    classes: str = ""
    properties: str = ""


@dataclass
class PaperResult:
    """A single academic paper result linked to an ontology."""
    ontology_filename: str = ""
    search_query: str = ""
    source: str = ""           # "semantic_scholar", "openalex", "crossref", "doi_lookup"
    paper_id: str = ""
    title: str = ""
    authors: str = ""
    year: int = 0
    venue: str = ""
    citation_count: int = 0
    abstract: str = ""
    doi: str = ""
    url: str = ""
    is_open_access: bool = False
    fields_of_study: str = ""
    relevance_score: float = 0.0   # composite relevance score (0.0–1.0)
    match_type: str = ""           # exact_ontology, ontology_family, reuse_application, broad_domain


@dataclass
class WebResult:
    """A single DuckDuckGo web search result linked to an ontology."""
    ontology_filename: str = ""
    search_query: str = ""
    result_type: str = ""          # official_namespace, github_repo, paper, documentation, other
    title: str = ""
    url: str = ""
    snippet: str = ""
    relevance_score: float = 0.0
    match_type: str = ""           # exact_ontology, ontology_family, broad_domain


# ---------------------------------------------------------------------------
# Phase 1b – Evaluation metric models
# ---------------------------------------------------------------------------

@dataclass
class MetadataMetrics:
    """Per-ontology metadata quality and structural metrics (step 1)."""
    filename: str = ""
    fields_total: int = 13
    fields_filled: int = 0
    completeness_pct: float = 0.0
    parse_success: bool = True
    parse_error: str = ""
    class_count: int = 0
    property_count: int = 0
    axiom_count: int = 0
    annotation_coverage_pct: float = 0.0
    comment_coverage_pct: float = 0.0
    import_count: int = 0
    language_count: int = 0
    has_title: bool = False
    has_namespace_uri: bool = False
    has_description: bool = False
    has_license: bool = False
    has_version: bool = False
    has_creators: bool = False


@dataclass
class PaperSearchMetrics:
    """Per-ontology paper search relevance metrics (step 2)."""
    ontology_filename: str = ""
    search_query: str = ""
    results_returned: int = 0
    avg_title_keyword_overlap: float = 0.0
    avg_abstract_keyword_overlap: float = 0.0
    max_title_overlap: float = 0.0
    doi_direct_hit: bool = False
    avg_citation_count: float = 0.0
    median_year: int = 0
    cs_engineering_ratio: float = 0.0
    open_access_ratio: float = 0.0


@dataclass
class WebSearchMetrics:
    """Per-ontology web search relevance metrics (step 3)."""
    ontology_filename: str = ""
    search_query: str = ""
    results_returned: int = 0
    avg_title_keyword_overlap: float = 0.0
    avg_snippet_keyword_overlap: float = 0.0
    unique_domains: int = 0
    github_result_count: int = 0
    academic_result_count: int = 0


@dataclass
class ConsolidatedOntologyRow:
    """One ontology with metadata, output counts, and top linked evidence."""
    filename: str = ""
    title: str = ""
    prefix: str = ""
    namespace_uri: str = ""
    description: str = ""
    parse_success: bool = False
    metadata_completeness_pct: float = 0.0
    class_count: int = 0
    property_count: int = 0
    axiom_count: int = 0
    annotation_coverage_pct: float = 0.0
    comment_coverage_pct: float = 0.0
    has_license: bool = False
    has_version: bool = False
    has_creators: bool = False
    has_embedded_doi: bool = False
    paper_count: int = 0
    top_paper_titles: str = ""
    top_paper_urls: str = ""
    paper_match_types: str = ""
    web_result_count: int = 0
    top_web_titles: str = ""
    top_web_urls: str = ""
    web_result_types: str = ""


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def save_csv(rows: Sequence, filepath: str | Path) -> None:
    """Write a list of dataclass instances to a CSV file."""
    if not rows:
        return
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    field_names = [f.name for f in fields(rows[0])]
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=field_names)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    print(f"  Saved {len(rows)} rows -> {filepath}")


def save_json(rows: Sequence, filepath: str | Path) -> None:
    """Write a list of dataclass instances to a JSON file."""
    if not rows:
        return
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in rows], fh, indent=2, ensure_ascii=False)
    print(f"  Saved {len(rows)} records -> {filepath}")
