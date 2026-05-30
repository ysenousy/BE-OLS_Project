"""
step2_search_papers.py – Phase 3: Academic Paper Search

Queries free APIs for each ontology in data/ontology_metadata.json:
  1. OpenAlex    – broadest coverage, fast (10 req/s polite pool)
  2. CrossRef    – DOI resolution only (resolves embedded DOIs from rdfs:seeAlso)

Results are deduplicated by DOI (or title if no DOI) and merged.
Computes per-ontology relevance metrics.

Outputs:
  Files are written under the data/ directory.
  data/ontology_papers.csv / .json               – paper results per ontology
  data/ontology_papers_metrics.csv / .json       – search relevance metrics
"""

from __future__ import annotations

import argparse
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
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESULTS_PER_QUERY = 10
POLITE_EMAIL = "be-ols-research@example.com"  # for OpenAlex/CrossRef polite pool

PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"
METADATA_FILE = DATA_DIR / "ontology_metadata.json"
SEARCH_PROFILE_FILE = DATA_DIR / "ontology_search_profiles.json"

# Delays (seconds)
OPENALEX_DELAY = float(os.environ.get("OPENALEX_DELAY", "2.0"))
CROSSREF_DELAY = float(os.environ.get("CROSSREF_DELAY", "0.5"))

HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,]+", re.IGNORECASE)

# Relevance filtering
MIN_RELEVANCE_SCORE = 0.05   # drop papers scoring below this
TOP_K_PER_ONTOLOGY = 10      # keep at most this many per ontology (after dedup)


# ---------------------------------------------------------------------------
# Silent incremental save (no print)
# ---------------------------------------------------------------------------

def _save_quiet(rows: Sequence, csv_path: Path, json_path: Path,
                row_type: type | None = None) -> None:
    """Overwrite CSV + JSON without printing (used for incremental saves)."""
    if rows:
        field_names = [f.name for f in fields(rows[0])]
    elif row_type is not None:
        field_names = [f.name for f in fields(row_type)]
    else:
        return
    csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    json_tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=field_names)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    with open(json_tmp, "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in rows], fh, indent=2, ensure_ascii=False)
    os.replace(csv_tmp, csv_path)
    os.replace(json_tmp, json_path)


def _load_dataclass_rows(path: Path, cls):
    """Load a JSON list into dataclass rows, ignoring any unknown fields."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw_rows = json.load(fh)
    allowed = {f.name for f in fields(cls)}
    return [cls(**{k: v for k, v in row.items() if k in allowed}) for row in raw_rows]


def _load_search_profiles() -> dict:
    """Load optional per-ontology search profiles from JSON."""
    if not SEARCH_PROFILE_FILE.exists():
        return {}
    with open(SEARCH_PROFILE_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return {}
    return data


def _profile_for(ontology: dict, profiles: dict | None = None) -> dict:
    profiles = profiles or {}
    filename = (ontology.get("filename") or "").strip()
    return profiles.get(filename, {}) if filename else {}


def _configure_stdout() -> None:
    """Avoid Windows cp1252 crashes when printing Unicode progress text."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search academic papers for BE-OLS ontology metadata."
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start from scratch instead of resuming from existing paper outputs.",
    )
    parser.add_argument(
        "--skip-doi",
        action="store_true",
        help="Skip DOI resolution from see_also links.",
    )
    parser.add_argument(
        "--openalex-only",
        action="store_true",
        help="Fast mode: use OpenAlex only, skipping DOI lookups.",
    )
    parser.add_argument(
        "--max-ontologies",
        type=int,
        default=None,
        help="Process at most this many new ontologies in this run.",
    )
    parser.add_argument(
        "--max-queries-per-ontology",
        type=int,
        default=3,
        help="Maximum OpenAlex query variants to run per ontology.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Shared HTTP helper
# ---------------------------------------------------------------------------

def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text_value(value) -> str:
    if isinstance(value, dict):
        return str(value.get("$", "") or "").strip()
    return str(value or "").strip()


def _attr_value(value, attr: str) -> str:
    if isinstance(value, dict):
        return str(value.get(attr, "") or "").strip()
    return ""


def _preferred_text_value(value, classid: str | None = None) -> str:
    items = _as_list(value)
    if classid:
        for item in items:
            if _attr_value(item, "@classid").lower() == classid.lower():
                text = _text_value(item)
                if text:
                    return text
    for item in items:
        text = _text_value(item)
        if text:
            return text
    return ""


def _get(url: str, params: dict | None = None,
         headers: dict | None = None, delay: float = 1.0,
         label: str = "") -> Optional[dict]:
    """GET JSON with retries + exponential backoff on 429."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers or {}, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = delay * (2 ** (attempt + 3))
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                print(f"    [{label}] 429 rate-limited, waiting {wait:.0f}s ...")
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
        "select": "id,doi,title,type,authorships,publication_year,primary_location,cited_by_count,abstract_inverted_index,open_access,concepts",
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
    type_label = work.get("type", "") or ""
    type_prefix = f"publication_type:{type_label}" if type_label else ""
    fos_parts = [type_prefix] if type_prefix else []
    fos_parts.extend(c.get("display_name", "") for c in concepts[:5])
    fos = ", ".join(part for part in fos_parts if part)

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
# 2. CrossRef  (DOI resolution + search)
# ===================================================================

CROSSREF_WORKS = "https://api.crossref.org/works"


def _crossref_headers() -> dict:
    return {"User-Agent": f"BE-OLS-Research/1.0 (mailto:{POLITE_EMAIL})"}


def search_crossref(query: str) -> List[dict]:
    """Search CrossRef works by query string."""
    params = {
        "query": query,
        "rows": RESULTS_PER_QUERY,
        "select": "DOI,type,title,author,published-print,published-online,container-title,is-referenced-by-count,abstract,URL,subject",
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
    type_label = item.get("type", "") or ""
    type_prefix = f"publication_type:{type_label}" if type_label else ""
    subject_parts = [type_prefix] if type_prefix else []
    subject_parts.extend(subjects[:5])

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
        fields_of_study=", ".join(subject_parts),
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

# Ontology/method terms and built-environment terms used for the relevance boost.
# Matching is whole-term based so short abbreviations such as "ifc" and "gis" do
# not fire inside unrelated words.
_ONTOLOGY_KEYWORDS = {
    "ontology", "ontologies", "semantic", "semantic web", "linked data",
    "rdf", "rdfs", "owl", "knowledge graph", "metadata", "interoperability",
}

_BE_DOMAIN_KEYWORDS = {
    "aec", "architecture", "architectural", "beam", "bim", "brick", "bridge",
    "building", "buildings", "city", "column", "concrete", "construction",
    "cooling", "district", "door", "electrical", "energy", "envelope",
    "facade", "facility", "floor", "geospatial", "gis", "grid", "heating",
    "hvac", "ifc", "indoor", "infrastructure", "land use", "lighting",
    "masonry", "mep", "occupancy", "occupant", "photovoltaic", "plumbing",
    "rebar", "renovation", "retrofit", "roof", "room", "sensor", "slab",
    "smart building", "smart grid", "smart home", "solar", "steel", "storey",
    "structural", "thermal", "timber", "urban", "ventilation", "wall",
    "window", "zone",
}

_DOMAIN_KEYWORDS = _ONTOLOGY_KEYWORDS | _BE_DOMAIN_KEYWORDS


def _keyword_pattern(term: str) -> str:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return rf"\b{escaped}\b"


_DOMAIN_KEYWORD_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_DOMAIN_KEYWORDS, key=len, reverse=True)),
    re.IGNORECASE,
)
_BE_DOMAIN_KEYWORD_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_BE_DOMAIN_KEYWORDS, key=len, reverse=True)),
    re.IGNORECASE,
)

_EXCLUDED_PAPER_TERMS = {
    "anatomy", "apoptosis", "bioinformatics", "biology", "biomedical",
    "cancer", "cell", "clinical", "cognition", "cognitive science",
    "consciousness", "disease", "drug", "ego", "gene", "gene expression",
    "genetics", "genomic", "health care", "healthcare", "hospital",
    "human health", "lymphocyte", "medical", "medicine", "molecular",
    "neuroscience", "nucleic acid", "patient", "pharma", "philosophy",
    "protein", "psychedelic", "psychology", "single-cell", "stem cell",
    "systems biology",
}

_EXCLUDED_PAPER_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_EXCLUDED_PAPER_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)

_ALLOWED_PUBLICATION_TYPES = {
    "article", "journal-article", "proceedings-article", "conference",
    "conference-paper", "paper-conference", "posted-content", "preprint",
}
_EXCLUDED_PUBLICATION_TYPES = {
    "book", "book-chapter", "book-section", "book-series", "component",
    "dataset", "dissertation", "edited-book", "monograph", "other",
    "peer-review", "proceedings", "reference-book", "reference-entry",
    "project-deliverable", "report", "standard", "training-material",
}
_EXCLUDED_PUBLICATION_TERMS = {
    "book", "book chapter", "chapter", "dissertation", "doctoral thesis",
    "edited volume", "lecture notes", "master thesis", "phd thesis",
    "project deliverable", "proceedings volume", "repository", "report",
    "training material", "thesis",
}
_EXCLUDED_PUBLICATION_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_EXCLUDED_PUBLICATION_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)


_CONTEXTUAL_QUERY_TERMS = {
    "damage", "energy", "grid", "iot", "material", "renewable", "safety",
    "sensor", "solar", "space", "topology", "water", "weather",
}
_STRONG_QUERY_TERMS = _BE_DOMAIN_KEYWORDS - _CONTEXTUAL_QUERY_TERMS
_STRONG_QUERY_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_STRONG_QUERY_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)
_CONTEXTUAL_QUERY_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_CONTEXTUAL_QUERY_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)

_QUERY_STOP_TERMS = {
    "about", "agent", "agents", "area", "areas", "assessment", "built",
    "environment", "interest", "interests", "model", "models", "ontology",
    "ontologies", "project", "system", "systems", "the", "with",
    "com", "net", "org", "http", "https", "www", "w3id", "lbd",
    "adopt", "adopted", "application", "applied", "based", "employ",
    "employed", "extend", "extended", "extension", "integrate", "integrated",
    "reuse", "reused", "using",
}

_GENERIC_TITLE_TERMS = {
    "annotation", "annotations", "area", "areas", "assessment", "building",
    "data", "element", "elements", "information", "interest", "interests",
    "management", "object", "objects", "process", "product", "quality",
    "resource", "resources", "system", "systems",
}

_BE_CONTEXT_FIELDS = ("description", "classes", "properties", "imports", "namespace_uri")

_REUSE_TERMS = {
    "adopt", "adopted", "application", "applied", "based on", "built on",
    "case study", "employ", "employed", "extend", "extended", "extension",
    "integrate", "integrated", "reuse", "reused", "using",
}
_REUSE_PATTERN = re.compile(
    "|".join(_keyword_pattern(term) for term in sorted(_REUSE_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)

_SHORT_PREFIX_LEN = 4


def _has_domain_context(text: str) -> bool:
    """Return True if text is already scoped to the built-environment domain."""
    if _STRONG_QUERY_PATTERN.search(text):
        return True
    contextual_hits = {m.group(0).lower() for m in _CONTEXTUAL_QUERY_PATTERN.finditer(text)}
    return len(contextual_hits) >= 2


def _has_ontology_mention(text: str) -> bool:
    low = text.lower()
    return "ontology" in low or "ontologies" in low or "owl" in low or "semantic web" in low


def _paper_text(paper: PaperResult) -> str:
    return f"{paper.title} {paper.abstract} {paper.venue} {paper.fields_of_study}"


def _publication_types(paper: PaperResult) -> set[str]:
    types = set()
    for match in re.findall(r"publication_type:([^,]+)", paper.fields_of_study or "", re.IGNORECASE):
        for part in re.split(r"[|;/]", match):
            cleaned = part.strip().lower().replace("_", "-").replace(" ", "-")
            if cleaned:
                types.add(cleaned)
    return types


def _is_research_paper_type(paper: PaperResult) -> bool:
    if paper.source == "doi_lookup":
        return True

    pub_types = _publication_types(paper)
    if pub_types:
        if pub_types & _EXCLUDED_PUBLICATION_TYPES:
            return False
        if pub_types & _ALLOWED_PUBLICATION_TYPES:
            return True

    text = _paper_text(paper)
    if _EXCLUDED_PUBLICATION_PATTERN.search(text):
        return False
    return True


def _be_domain_hits(paper: PaperResult) -> set[str]:
    return {m.group(0).lower() for m in _BE_DOMAIN_KEYWORD_PATTERN.finditer(_paper_text(paper))}


def _has_be_domain_evidence(paper: PaperResult) -> bool:
    """Require positive built-environment evidence in API search results."""
    if paper.source == "doi_lookup":
        return True
    return bool(_be_domain_hits(paper))


def _is_excluded_paper(paper: PaperResult) -> bool:
    """Drop obvious biomedical/clinical false positives from broad API search."""
    if paper.source == "doi_lookup":
        return False
    return bool(_EXCLUDED_PAPER_PATTERN.search(_paper_text(paper)))


def _base_query_text(ontology: dict) -> str:
    title = (ontology.get("title") or "").strip()
    prefix = (ontology.get("prefix") or "").strip()
    desc = (ontology.get("description") or "").strip()
    filename = (ontology.get("filename") or "").strip()

    if title:
        core = title
    elif desc:
        first_sentence = re.split(r"[.\n]", desc)[0].strip()
        if len(first_sentence) > 100:
            first_sentence = first_sentence[:100].rsplit(" ", 1)[0]
        core = first_sentence
    elif prefix:
        core = prefix
    else:
        core = filename.replace(".ttl", "").replace("_", " ").replace("-", " ")

    return core


def _identity_terms(ontology: dict) -> set[str]:
    terms = set()
    filename = (ontology.get("filename") or "").strip()
    title = (ontology.get("title") or "").strip()
    prefix = (ontology.get("prefix") or "").strip()
    namespace = (ontology.get("namespace_uri") or "").strip()

    for value in (prefix, filename.replace(".ttl", ""), title):
        value = value.strip()
        if not value:
            continue
        low = value.lower()
        if len(low) >= _SHORT_PREFIX_LEN and low not in _QUERY_STOP_TERMS:
            terms.add(low)
        compact = re.sub(r"[^a-z0-9]+", "", low)
        if len(compact) >= _SHORT_PREFIX_LEN and compact not in _QUERY_STOP_TERMS:
            terms.add(compact)

    namespace_bits = [
        bit.lower()
        for bit in re.split(r"[/#:_\-.]+", namespace)
        if len(bit) >= 3 and bit.lower() not in _QUERY_STOP_TERMS
    ]
    terms.update(bit for bit in namespace_bits[-3:] if len(bit) >= _SHORT_PREFIX_LEN)
    if namespace:
        terms.add(namespace.lower().rstrip("/#"))

    return terms


def _namespace_search_terms(ontology: dict) -> List[str]:
    namespace = (ontology.get("namespace_uri") or "").strip().rstrip("/#")
    if not namespace:
        return []
    terms = [namespace]
    if namespace.startswith("https://"):
        terms.append(namespace.replace("https://", "http://", 1))
    elif namespace.startswith("http://"):
        terms.append(namespace.replace("http://", "https://", 1))
    return terms


def _metadata_context_text(ontology: dict) -> str:
    return " ".join((ontology.get(field) or "") for field in _BE_CONTEXT_FIELDS)


def _metadata_domain_terms(ontology: dict) -> set[str]:
    context = _metadata_context_text(ontology)
    return {
        m.group(0).lower()
        for m in _BE_DOMAIN_KEYWORD_PATTERN.finditer(context)
        if len(m.group(0)) >= _SHORT_PREFIX_LEN
    }


def _significant_title_terms(ontology: dict) -> set[str]:
    title = (ontology.get("title") or "").lower()
    return {
        term
        for term in re.findall(r"[a-z][a-z0-9]{3,}", title)
        if term not in _QUERY_STOP_TERMS and term not in _GENERIC_TITLE_TERMS
    }


def _is_generic_ontology_name(ontology: dict) -> bool:
    title = (ontology.get("title") or "").strip()
    if not title:
        return True
    return len(_significant_title_terms(ontology)) == 0


def _profile_list(profile: dict, key: str) -> List[str]:
    values = profile.get(key, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def derive_search_query(ontology: dict, profile: dict | None = None) -> str:
    """Build a readable primary paper-search query from Step 1 metadata."""
    profile_queries = _profile_list(profile or {}, "queries")
    if profile_queries:
        return profile_queries[0]

    core = _base_query_text(ontology)
    prefix = (ontology.get("prefix") or "").strip()
    if not core:
        return ""
    if not _has_ontology_mention(core):
        core = f"{core} ontology"
    if not _has_domain_context(core):
        core = f"{core} built environment"
    parts = [core]
    if prefix and prefix.lower() not in core.lower():
        parts.append(f"{prefix} ontology")
    return " | ".join(parts)


def derive_search_queries(ontology: dict, profile: dict | None = None) -> List[str]:
    """Build ordered search queries, from exact ontology-specific to broader."""
    title = (ontology.get("title") or "").strip()
    prefix = (ontology.get("prefix") or "").strip()
    base = _base_query_text(ontology)
    readable = derive_search_query(ontology, profile).split(" | ")[0].strip()
    candidates = _profile_list(profile or {}, "queries")

    for term in (title, prefix):
        if term and (term != prefix or len(prefix) >= _SHORT_PREFIX_LEN):
            candidates.append(f'"{term}" ontology')
    for namespace in _namespace_search_terms(ontology):
        candidates.append(f'"{namespace}"')
    for term in (title, prefix):
        if term and (term != prefix or len(prefix) >= _SHORT_PREFIX_LEN):
            for reuse_term in ("using", "reuse", "extended", "application"):
                candidates.append(f'"{term}" {reuse_term}')
    if title and not _has_domain_context(title):
        candidates.append(f'"{title}" built environment ontology')
    if base and base != title:
        candidates.append(base)
    if readable:
        candidates.append(readable)

    unique = []
    seen = set()
    for q in candidates:
        q = q.strip()
        key = q.lower()
        if q and key not in seen:
            unique.append(q)
            seen.add(key)
    return unique


def _query_terms(query: str) -> set[str]:
    terms = {
        t.lower()
        for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", query)
        if len(t) >= _SHORT_PREFIX_LEN and t.lower() not in _QUERY_STOP_TERMS
    }
    quoted = re.findall(r'"([^"]+)"', query)
    for phrase in quoted:
        phrase_terms = {
            t.lower()
            for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", phrase)
            if len(t) >= _SHORT_PREFIX_LEN and t.lower() not in _QUERY_STOP_TERMS
        }
        if phrase_terms:
            terms.add(phrase.lower())
    return terms


def _has_identity_evidence(ontology: dict, paper: PaperResult) -> bool:
    text = _paper_text(paper).lower()
    for term in _identity_terms(ontology):
        if term.startswith("http"):
            if term in text:
                return True
            continue
        if re.search(_keyword_pattern(term), text, re.IGNORECASE):
            return True
    return False


def _has_namespace_evidence(ontology: dict, paper: PaperResult) -> bool:
    text = _paper_text(paper).lower()
    return any(term in text for term in _namespace_search_terms(ontology))


def _has_reuse_evidence(paper: PaperResult) -> bool:
    return bool(_REUSE_PATTERN.search(_paper_text(paper)))


def _has_metadata_context_evidence(ontology: dict, paper: PaperResult) -> bool:
    text = _paper_text(paper).lower()
    hits = 0
    for term in _metadata_domain_terms(ontology):
        if re.search(_keyword_pattern(term), text, re.IGNORECASE):
            hits += 1
    return hits >= 1


def _profile_all_terms_satisfied(profile: dict, paper: PaperResult) -> bool:
    required_all = _profile_list(profile, "required_terms_all")
    if not required_all:
        return True
    text = _paper_text(paper).lower()
    return all(_text_has_profile_term(text, term) for term in required_all)


def _has_profile_any_evidence(profile: dict, paper: PaperResult) -> bool:
    terms = _profile_list(profile, "required_terms") + _profile_list(profile, "optional_terms")
    if not terms:
        return False
    text = _paper_text(paper).lower()
    return any(_text_has_profile_term(text, term) for term in terms)


def _profile_term_hit_count(profile: dict, paper: PaperResult, key: str) -> int:
    text = _paper_text(paper).lower()
    return sum(1 for term in _profile_list(profile, key) if _text_has_profile_term(text, term))


def _has_profile_family_evidence(profile: dict, paper: PaperResult) -> bool:
    if not profile:
        return False
    if not _profile_all_terms_satisfied(profile, paper):
        return False
    if _profile_term_hit_count(profile, paper, "required_terms") >= 1:
        return True
    return _profile_term_hit_count(profile, paper, "optional_terms") >= 2


def _has_exact_identity_evidence(ontology: dict, paper: PaperResult) -> bool:
    """Return True only for strong ontology identity evidence."""
    if _has_namespace_evidence(ontology, paper):
        return True

    text = _paper_text(paper).lower()
    title = (ontology.get("title") or "").strip()
    prefix = (ontology.get("prefix") or "").strip()

    if title and not _is_generic_ontology_name(ontology):
        if re.search(_keyword_pattern(title), text, re.IGNORECASE):
            return True

    if prefix and len(prefix) >= _SHORT_PREFIX_LEN and prefix.lower() not in _QUERY_STOP_TERMS:
        if re.search(_keyword_pattern(prefix), text, re.IGNORECASE):
            return True

    return False


def _text_has_profile_term(text: str, term: str) -> bool:
    term = term.strip().lower()
    if not term:
        return False
    if term.startswith("http"):
        return term.rstrip("/#") in text
    return bool(re.search(_keyword_pattern(term), text, re.IGNORECASE))


def _has_profile_required_evidence(profile: dict, paper: PaperResult) -> bool:
    if _paper_matches_profile_doi(profile, paper):
        return True
    if not _profile_all_terms_satisfied(profile, paper):
        return False
    required_terms = _profile_list(profile, "required_terms")
    if not required_terms:
        return True
    text = _paper_text(paper).lower()
    return any(_text_has_profile_term(text, term) for term in required_terms)


def _paper_matches_profile_doi(profile: dict, paper: PaperResult) -> bool:
    known_dois = {
        doi.lower().removeprefix("https://doi.org/")
        for doi in _profile_list(profile, "known_dois")
    }
    if not known_dois or not paper.doi:
        return False
    paper_doi = paper.doi.lower().removeprefix("https://doi.org/")
    return paper_doi in known_dois


def _is_excluded_by_profile(profile: dict, paper: PaperResult) -> bool:
    excluded_terms = _profile_list(profile, "exclude_terms")
    if not excluded_terms:
        return False
    text = _paper_text(paper).lower()
    return any(_text_has_profile_term(text, term) for term in excluded_terms)


def _passes_ontology_context(ontology: dict | None, paper: PaperResult,
                             profile: dict | None = None) -> bool:
    return _classify_match(ontology, paper, profile) in {
        "exact_ontology", "ontology_family", "reuse_application"
    }


def _classify_match(ontology: dict | None, paper: PaperResult,
                    profile: dict | None = None) -> str:
    profile = profile or {}
    if paper.source == "doi_lookup" or _paper_matches_profile_doi(profile, paper):
        return "exact_ontology"
    if ontology and _has_exact_identity_evidence(ontology, paper):
        return "exact_ontology"

    family_evidence = _has_profile_family_evidence(profile, paper)
    if family_evidence and (_has_reuse_evidence(paper) or _has_ontology_mention(_paper_text(paper))):
        return "reuse_application" if _has_reuse_evidence(paper) else "ontology_family"
    if family_evidence:
        return "ontology_family"
    if _has_be_domain_evidence(paper) and _has_ontology_mention(_paper_text(paper)):
        return "broad_domain"
    return "none"


def _has_query_specific_evidence(query: str, paper: PaperResult,
                                 ontology: dict | None = None,
                                 profile: dict | None = None) -> bool:
    if _classify_match(ontology, paper, profile) in {
        "exact_ontology", "ontology_family", "reuse_application"
    }:
        return True
    if ontology and _has_exact_identity_evidence(ontology, paper):
        return True
    text = _paper_text(paper).lower()
    return any(term in text for term in _query_terms(query))


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
    combined = _paper_text(paper)
    if combined.strip():
        domain_hits = {m.group(0).lower() for m in _DOMAIN_KEYWORD_PATTERN.finditer(combined)}
        domain_score = min(len(domain_hits) / 5.0, 1.0)  # cap at 5 hits -> 1.0
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


def score_and_filter(query: str, papers: List[PaperResult],
                     ontology: dict | None = None,
                     profile: dict | None = None) -> List[PaperResult]:
    """Score papers, drop those below threshold, sort by relevance, keep top-K."""
    profile = profile or {}
    filtered = []
    for paper in papers:
        paper.match_type = _classify_match(ontology, paper, profile)
        if paper.source == "doi_lookup":
            filtered.append(paper)
            continue
        if (
            _is_research_paper_type(paper)
            and not _is_excluded_paper(paper)
            and not _is_excluded_by_profile(profile, paper)
            and _has_query_specific_evidence(query, paper, ontology, profile)
            and paper.match_type in {"exact_ontology", "ontology_family", "reuse_application"}
        ):
            filtered.append(paper)
    papers = filtered

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
    profiles = _load_search_profiles()

    print(f"Loaded {len(ontologies)} ontologies from {METADATA_FILE.name}")
    if profiles:
        print(f"Loaded {len(profiles)} ontology search profiles from {SEARCH_PROFILE_FILE.name}")
    print("APIs: OpenAlex (search), CrossRef (DOI resolution)\n")

    all_papers: List[PaperResult] = []
    all_metrics: List[PaperSearchMetrics] = []

    for i, ont in enumerate(ontologies, 1):
        filename = ont["filename"]
        profile = _profile_for(ont, profiles)
        query = derive_search_query(ont, profile)
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

        # --- 2. DOI look-ups from rdfs:seeAlso (CrossRef) ---
        doi_hit = False
        dois = sorted(set(extract_dois(see_also) + _profile_list(profile, "known_dois")))
        seen_dois: Set[str] = {p.doi.lower() for p in papers if p.doi}
        for doi in dois:
            if doi.lower() in seen_dois:
                doi_hit = True
                continue
            # Try CrossRef for DOI metadata.
            time.sleep(CROSSREF_DELAY)
            cr_data = lookup_doi_crossref(doi)
            if cr_data:
                doi_hit = True
                papers.append(_crossref_to_paper(filename, f"DOI:{doi}", cr_data, src="doi_lookup"))

        # --- 4. Deduplicate ---
        papers = deduplicate_papers(papers)

        # --- 5. Score & filter by relevance ---
        before_filter = len(papers)
        papers = score_and_filter(primary_query, papers, ont, profile)
        all_papers.extend(papers)

        # --- 6. Metrics ---
        met = compute_metrics(filename, primary_query, papers, doi_hit)
        all_metrics.append(met)
        print(f"    → {before_filter} unique → {len(papers)} relevant (top-{TOP_K_PER_ONTOLOGY}, ≥{MIN_RELEVANCE_SCORE}), DOI hit: {doi_hit}")
        if papers:
            print(f"      best: [{papers[0].relevance_score:.3f}] {papers[0].title[:70]}")

        # --- 7. Incremental save (overwrite after each ontology) ---
        _save_quiet(all_papers, DATA_DIR / "ontology_papers.csv", DATA_DIR / "ontology_papers.json", PaperResult)
        _save_quiet(all_metrics, DATA_DIR / "ontology_papers_metrics.csv", DATA_DIR / "ontology_papers_metrics.json", PaperSearchMetrics)

    # Final summary
    print(f"\nTotal: {len(all_papers)} paper results across {len(all_metrics)} ontologies")
    print_metrics_summary(all_metrics)

    print("Done ✓")


def main_resumable() -> None:
    _configure_stdout()
    args = parse_args()
    if args.openalex_only:
        args.skip_doi = True

    if not METADATA_FILE.exists():
        print(f"ERROR: {METADATA_FILE} not found. Run step1_extract_metadata.py first.")
        sys.exit(1)

    with open(METADATA_FILE, "r", encoding="utf-8") as fh:
        ontologies = json.load(fh)
    profiles = _load_search_profiles()

    print(f"Loaded {len(ontologies)} ontologies from {METADATA_FILE.name}")
    if profiles:
        print(f"Loaded {len(profiles)} ontology search profiles from {SEARCH_PROFILE_FILE.name}")
    enabled_apis = ["OpenAlex"]
    if not args.skip_doi:
        enabled_apis.append("CrossRef DOI resolution")
    print(f"APIs enabled: {', '.join(enabled_apis)}")
    print(
        f"Retries: {MAX_RETRIES}, HTTP timeout: {HTTP_TIMEOUT:g}s, "
        f"delays: OpenAlex={OPENALEX_DELAY:g}s, "
        f"CrossRef={CROSSREF_DELAY:g}s, "
        f"OpenAlex queries/ontology={args.max_queries_per_ontology}\n"
    )

    papers_csv = DATA_DIR / "ontology_papers.csv"
    papers_json = DATA_DIR / "ontology_papers.json"
    metrics_csv = DATA_DIR / "ontology_papers_metrics.csv"
    metrics_json = DATA_DIR / "ontology_papers_metrics.json"

    all_papers: List[PaperResult] = []
    all_metrics: List[PaperSearchMetrics] = []
    processed: Set[str] = set()
    if not args.fresh:
        all_papers = _load_dataclass_rows(papers_json, PaperResult)
        all_metrics = _load_dataclass_rows(metrics_json, PaperSearchMetrics)
        processed = {m.ontology_filename for m in all_metrics}
        if processed:
            print(
                f"Resume mode: loaded {len(all_papers)} papers and "
                f"{len(all_metrics)} metrics; {len(processed)} ontologies already done."
            )
            print("Use --fresh to discard existing Step 02 outputs and rebuild.\n")

    new_processed = 0

    for i, ont in enumerate(ontologies, 1):
        filename = ont["filename"]
        profile = _profile_for(ont, profiles)
        query = derive_search_query(ont, profile)
        search_queries = derive_search_queries(ont, profile)
        if args.max_queries_per_ontology > 0:
            search_queries = search_queries[:args.max_queries_per_ontology]
        see_also = ont.get("see_also", "")

        if filename in processed:
            print(f"[{i}/{len(ontologies)}] {filename} - already processed, skipping")
            continue

        if args.max_ontologies is not None and new_processed >= args.max_ontologies:
            print(f"Reached --max-ontologies={args.max_ontologies}; stopping early.")
            break

        if not query:
            print(f"[{i}/{len(ontologies)}] {filename} - no search keywords, skipping")
            all_metrics.append(PaperSearchMetrics(ontology_filename=filename))
            processed.add(filename)
            new_processed += 1
            _save_quiet(all_papers, papers_csv, papers_json, PaperResult)
            _save_quiet(all_metrics, metrics_csv, metrics_json, PaperSearchMetrics)
            continue

        primary_query = search_queries[0] if search_queries else query.split(" | ")[0].strip()
        print(f"[{i}/{len(ontologies)}] {filename} - \"{primary_query}\"")

        papers: List[PaperResult] = []

        oa_total = 0
        for search_query in search_queries:
            time.sleep(OPENALEX_DELAY)
            oa_results = search_openalex(search_query)
            oa_papers = [_openalex_to_paper(filename, search_query, w) for w in oa_results]
            papers.extend(oa_papers)
            oa_total += len(oa_papers)

        print(f"    OpenAlex: {oa_total} results across {len(search_queries)} queries")

        doi_hit = False
        dois = [] if args.skip_doi else sorted(set(extract_dois(see_also) + _profile_list(profile, "known_dois")))
        seen_dois: Set[str] = {p.doi.lower() for p in papers if p.doi}
        for doi in dois:
            if doi.lower() in seen_dois:
                doi_hit = True
                continue
            time.sleep(CROSSREF_DELAY)
            cr_data = lookup_doi_crossref(doi)
            if cr_data:
                doi_hit = True
                papers.append(_crossref_to_paper(filename, f"DOI:{doi}", cr_data, src="doi_lookup"))

        papers = deduplicate_papers(papers)
        before_filter = len(papers)
        papers = score_and_filter(query, papers, ont, profile)
        all_papers.extend(papers)

        met = compute_metrics(filename, query, papers, doi_hit)
        all_metrics.append(met)
        processed.add(filename)
        new_processed += 1
        print(
            f"    -> {before_filter} unique -> {len(papers)} relevant "
            f"(top-{TOP_K_PER_ONTOLOGY}, >={MIN_RELEVANCE_SCORE}), DOI hit: {doi_hit}"
        )
        if papers:
            print(f"      best: [{papers[0].relevance_score:.3f}] {papers[0].title[:70]}")

        _save_quiet(all_papers, papers_csv, papers_json, PaperResult)
        _save_quiet(all_metrics, metrics_csv, metrics_json, PaperSearchMetrics)

    print(f"\nTotal: {len(all_papers)} paper results across {len(all_metrics)} ontologies")
    print_metrics_summary(all_metrics)
    print("Done")


if __name__ == "__main__":
    main_resumable()
