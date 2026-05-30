"""
step3_search_web.py - Phase 4: Web Search

Searches DuckDuckGo for web pages, tools, documentation, and project pages
related to each BE-OLS ontology.

Inputs:
  data/ontology_metadata.json
  data/ontology_search_profiles.json (optional)

Outputs:
  data/ontology_web_results.csv / .json
  data/ontology_web_metrics.csv / .json
"""

from __future__ import annotations

import argparse
import csv
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
import sys
import time
from dataclasses import asdict, fields
from pathlib import Path
from statistics import mean
from typing import List, Sequence, Set
from urllib.parse import urlparse

import requests

from common import WebResult, WebSearchMetrics, jaccard_similarity


PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"
METADATA_FILE = DATA_DIR / "ontology_metadata.json"
SEARCH_PROFILE_FILE = DATA_DIR / "ontology_search_profiles.json"

RESULTS_PER_QUERY = int(os.environ.get("WEB_RESULTS_PER_QUERY", "10"))
WEB_SEARCH_DELAY = float(os.environ.get("WEB_SEARCH_DELAY", "2.5"))
TITLE_FETCH_TIMEOUT = float(os.environ.get("TITLE_FETCH_TIMEOUT", "8"))

ACADEMIC_DOMAINS = (
    "doi.org",
    "researchgate.net",
    "semanticscholar.org",
    "scholar.google.",
    "springer.com",
    "sciencedirect.com",
    "ieeexplore.ieee.org",
    "acm.org",
    "mdpi.com",
    "frontiersin.org",
    "arxiv.org",
    "zenodo.org",
    "figshare.com",
    "ssrn.com",
    "publications.rwth-aachen.de",
)

EXCLUDED_DOMAINS = {
    "acronymfinder.com",
    "bizapedia.com",
    "con-tax.com",
    "con-taxservices.com",
    "contax.clientportal.com",
    "corporationwiki.com",
    "cyberbuildlab.github.io",
    "en.wikipedia.org",
    "facebook.com",
    "forum.aqara.com",
    "linkedin.com",
    "pinterest.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "wikipedia.org",
}

EXCLUDED_URL_PARTS = (
    "acronymfinder.com",
    "blogspot.com",
    "bizapedia.com/",
    "clientportal.com/",
    "con-tax.com/",
    "con-taxservices.com/",
    "constructiondigital.com/",
    "corporationwiki.com/",
    "cyberbuildlab.github.io/be-ols",
    "coreontology.com/",
    "github.com/cyberbuildlab/be-ols",
    "askocdek.blogspot.com",
    "forum.aqara.com/",
    "ontolearner.readthedocs.io/benchmarking",
    "youtube.com/",
    "youtu.be/",
)


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: List[str] = []
        self.meta_title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {name.lower(): value for name, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            if key in {"og:title", "twitter:title"} and attrs_dict.get("content"):
                self.meta_title = attrs_dict["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def _configure_stdout() -> None:
    """Avoid Windows cp1252 crashes when printing Unicode progress text."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search DuckDuckGo web results for BE-OLS ontology metadata."
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start from scratch instead of resuming from existing web outputs.",
    )
    parser.add_argument(
        "--max-ontologies",
        type=int,
        default=None,
        help="Process at most this many new ontologies in this run.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=RESULTS_PER_QUERY,
        help="Maximum web results to keep per ontology.",
    )
    parser.add_argument(
        "--max-queries-per-ontology",
        type=int,
        default=3,
        help="Maximum DuckDuckGo query variants to run per ontology.",
    )
    return parser.parse_args()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_dataclass_rows(path: Path, cls):
    if not path.exists():
        return []
    raw_rows = _load_json(path, [])
    allowed = {f.name for f in fields(cls)}
    return [cls(**{k: v for k, v in row.items() if k in allowed}) for row in raw_rows]


def _save_quiet(rows: Sequence, csv_path: Path, json_path: Path,
                row_type: type | None = None) -> None:
    if rows:
        field_names = [f.name for f in fields(rows[0])]
    elif row_type is not None:
        field_names = [f.name for f in fields(row_type)]
    else:
        return

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    json_tmp = json_path.with_suffix(json_path.suffix + ".tmp")

    with open(csv_tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=field_names)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    with open(json_tmp, "w", encoding="utf-8") as fh:
        json.dump([asdict(row) for row in rows], fh, indent=2, ensure_ascii=False)

    os.replace(csv_tmp, csv_path)
    os.replace(json_tmp, json_path)


def _profile_for(ontology: dict, profiles: dict) -> dict:
    filename = (ontology.get("filename") or "").strip()
    return profiles.get(filename, {}) if filename else {}


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    for sep in (". ", "; "):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    return text[:120].strip()


def _clean_namespace_marker(namespace: str) -> str:
    parsed = urlparse(namespace or "")
    path_parts = [part for part in parsed.path.strip("/#").split("/") if part]
    if parsed.fragment:
        path_parts.append(parsed.fragment)
    for marker in reversed(path_parts):
        marker = marker.strip().lower()
        if marker and marker not in {"def", "voc", "ontology", "ontologies", "core"}:
            return marker
    return ""


def _quote_query(text: str) -> str:
    text = " ".join((text or "").split())
    return f'"{text}"' if text else ""


def derive_web_queries(ontology: dict, profile: dict | None = None) -> List[str]:
    profile = profile or {}
    queries = [str(query).strip() for query in profile.get("queries", []) if str(query).strip()]
    title = (ontology.get("title") or "").strip()
    prefix = (ontology.get("prefix") or "").strip()
    namespace = (ontology.get("namespace_uri") or "").strip()
    namespace_marker = _clean_namespace_marker(namespace)
    description = _first_sentence(ontology.get("description") or "")

    if title:
        queries.append(f"{_quote_query(title)} ontology")
    if namespace_marker:
        queries.append(f"{_quote_query(namespace_marker)} ontology")
    if namespace:
        queries.append(_quote_query(namespace.rstrip("/#")))
    if prefix:
        queries.append(f"{_quote_query(prefix)} built environment ontology")
    if description:
        queries.append(f"{description} ontology")

    deduped: List[str] = []
    seen: Set[str] = set()
    for query in queries:
        key = query.lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped


def derive_web_query(ontology: dict, profile: dict | None = None) -> str:
    queries = derive_web_queries(ontology, profile)
    return queries[0] if queries else ""


def _get_ddgs_class():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError as exc:
            raise RuntimeError(
                "Install a DuckDuckGo search package first: pip install ddgs"
            ) from exc


def search_duckduckgo(query: str, max_results: int) -> List[dict]:
    DDGS = _get_ddgs_class()
    with DDGS() as ddgs:
        try:
            return list(ddgs.text(query=query, max_results=max_results) or [])
        except TypeError:
            return list(ddgs.text(keywords=query, max_results=max_results) or [])


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _dedup_key(result: WebResult) -> str:
    parsed = urlparse(result.url or "")
    domain = _domain(result.url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if domain == "github.com" and len(parts) >= 2:
        return f"github.com/{parts[0].lower()}/{parts[1].lower()}"
    return (result.url or result.title).strip().lower().rstrip("/")


def _is_academic_domain(domain: str) -> bool:
    return any(marker in domain for marker in ACADEMIC_DOMAINS)


def classify_result_type(url: str) -> str:
    domain = _domain(url)
    parsed = urlparse(url or "")
    if domain == "github.com":
        return "github_repo"
    if _is_academic_domain(domain) or parsed.path.lower().endswith(".pdf"):
        return "paper"
    if domain in {"w3id.org", "purl.org"}:
        return "official_namespace"
    if domain.endswith("linkeddata.es") or domain.endswith("github.io") or "ontology" in parsed.path.lower():
        return "documentation"
    return "other"


def _is_excluded_result(result: WebResult) -> bool:
    url = result.url.lower()
    domain = _domain(url)
    if (
        domain in EXCLUDED_DOMAINS
        or domain.endswith(".wikipedia.org")
        or domain.endswith(".linkedin.com")
        or domain.endswith(".youtube.com")
    ):
        return True
    return any(part in url for part in EXCLUDED_URL_PARTS)


def _result_url(result: dict) -> str:
    return str(result.get("href") or result.get("url") or "").strip()


def _result_snippet(result: dict) -> str:
    return _clean_snippet(str(result.get("body") or result.get("snippet") or ""))


def _clean_snippet(snippet: str) -> str:
    snippet = " ".join((snippet or "").split())
    snippet = re.sub(r"[\uac00-\ud7af]", "", snippet)
    snippet = re.sub(r"^[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\s*[-–]\s*", "", snippet)
    snippet = re.sub(r"\s*(?:\.\.\.|…)\s*", " ... ", snippet)
    snippet = " ".join(snippet.split()).strip()
    if len(snippet) <= 360:
        return snippet

    cut = snippet[:360]
    sentence_end = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end >= 160:
        return cut[:sentence_end + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + " ..."


def _is_low_value_snippet(snippet: str) -> bool:
    text = " ".join((snippet or "").lower().split())
    if not text:
        return True
    boilerplate_terms = (
        "acknowledgements back to toc",
        "acknowledgments back to toc",
        "silvio peroni",
        "daniel garijo",
        "live owl documentation environment",
        "widoco",
        "back to toc",
        "cross reference for",
        "cross-reference for",
        "back to toc this section provides details",
    )
    return any(term in text for term in boilerplate_terms)


def _improve_snippet(ontology: dict, result: WebResult) -> WebResult:
    if _is_low_value_snippet(result.snippet):
        result.snippet = _clean_snippet(
            ontology.get("description")
            or f"{result.title} documentation page."
        )
    return result


def _looks_truncated(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped.endswith("...") or stripped.endswith("…") or "..." in stripped or "…" in stripped


def _page_title(url: str) -> str:
    if not url:
        return ""
    try:
        response = requests.get(
            url,
            timeout=TITLE_FETCH_TIMEOUT,
            headers={"User-Agent": "BE-OLS-Research/1.0"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""

    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return ""

    parser = _TitleParser()
    parser.feed(response.text[:200000])
    title = parser.meta_title or " ".join(parser.title_parts)
    return " ".join(unescape(title).split())


def _display_title_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path.strip("/#")
    marker = parsed.fragment or (path.split("/")[-1] if path else parsed.netloc)
    return marker.replace("-", " ").replace("_", " ").strip()


def _expand_title_if_needed(title: str, url: str) -> str:
    if not _looks_truncated(title):
        return title
    expanded = _page_title(url)
    if not expanded or len(expanded) <= len(title.rstrip(".…")):
        return title
    if len(expanded) > 180:
        return title.rstrip(".…").strip()
    return expanded


def _seed_result_from_url(ontology: dict, url: str, query: str) -> WebResult | None:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None

    title = _page_title(url) or ontology.get("title") or _display_title_from_url(url)
    result = WebResult(
        ontology_filename=ontology.get("filename", ""),
        search_query=query,
        result_type=classify_result_type(url),
        title=title,
        url=url,
        snippet=_clean_snippet(
            ontology.get("description")
            or f"Ontology namespace or repository URL from metadata: {url}"
        ),
    )
    if _is_excluded_result(result):
        return None
    return result


def seed_metadata_results(ontology: dict, query: str) -> List[WebResult]:
    namespace = (ontology.get("namespace_uri") or "").strip()
    urls: List[str] = []
    if namespace:
        urls.append(namespace)
        if namespace.endswith("#"):
            urls.append(namespace[:-1])

        parsed = urlparse(namespace)
        if _domain(namespace) == "github.com":
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) >= 2:
                urls.append(f"https://github.com/{parts[0]}/{parts[1]}")

    results: List[WebResult] = []
    seen: Set[str] = set()
    for url in urls:
        key = url.rstrip("/#").lower()
        if key in seen:
            continue
        seen.add(key)
        result = _seed_result_from_url(ontology, url, query)
        if result:
            results.append(result)
    return results


def _text_has(text: str, term: str) -> bool:
    term = " ".join((term or "").lower().split())
    if not term:
        return False
    haystack = " ".join((text or "").lower().split())
    if len(term) <= 4:
        return bool(re.search(rf"\b{re.escape(term)}\b", haystack))
    return term in haystack


def _has_acronym_or_digit(text: str) -> bool:
    for token in re.findall(r"[A-Za-z0-9]+", text or ""):
        if any(char.isdigit() for char in token):
            return True
        if len(token) > 1 and token.isupper():
            return True
        if len(token) > 2 and token != token.lower() and token != token.capitalize():
            return True
    return False


def _is_generic_ontology_title(title: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9]+", title or "")
    stopwords = {"a", "an", "and", "for", "in", "of", "on", "the", "to"}
    lowered = {token.lower() for token in tokens if token.lower() not in stopwords}
    generic_domain_terms = {
        "area", "areas", "assessment", "building", "buildings", "circularity",
        "collaboration", "element", "elements", "format", "interest",
        "interests", "object", "objects", "ontology", "ontologies",
    }
    if not tokens:
        return True
    if _has_acronym_or_digit(title):
        return False
    if any(token in {"ontology", "ontologies"} for token in lowered) and len(tokens) <= 4:
        return True
    return len(lowered) <= 4 and lowered.issubset(generic_domain_terms)


def _strong_identifiers(ontology: dict, profile: dict | None = None) -> List[str]:
    identifiers: List[str] = []
    title = (ontology.get("title") or "").strip()
    prefix = (ontology.get("prefix") or "").strip()
    namespace = (ontology.get("namespace_uri") or "").strip()
    namespace_marker = _clean_namespace_marker(namespace)
    title_value = "" if _is_generic_ontology_title(title) else title

    prefix_value = prefix if len(prefix) >= 5 or _has_acronym_or_digit(prefix) else ""
    namespace_marker_value = namespace_marker if len(namespace_marker) >= 5 else ""

    for value in (title_value, prefix_value, namespace, namespace_marker_value):
        if value and len(value) >= 3:
            identifiers.append(value)

    generic = {"ontology", "ontologies", "building", "renovation", "construction"}
    deduped: List[str] = []
    seen: Set[str] = set()
    for identifier in identifiers:
        key = identifier.lower().strip().strip('"')
        if key in generic or key in seen:
            continue
        seen.add(key)
        deduped.append(identifier)
    return deduped


def score_web_result(ontology: dict, profile: dict, query: str, result: WebResult) -> WebResult:
    result = _improve_snippet(ontology, result)
    text = f"{result.title} {result.url} {result.snippet}"
    primary_text = f"{result.title} {result.url}"
    identifiers = _strong_identifiers(ontology, profile)
    primary_hits = [identifier for identifier in identifiers if _text_has(primary_text, identifier)]
    snippet_hits = [identifier for identifier in identifiers if _text_has(text, identifier)]
    title_overlap = jaccard_similarity(query, result.title)
    snippet_overlap = jaccard_similarity(query, result.snippet)

    if primary_hits:
        result.match_type = "exact_ontology"
        result.relevance_score = min(1.0, 0.65 + 0.1 * len(primary_hits) + 0.15 * title_overlap)
    elif snippet_hits and _is_academic_domain(_domain(result.url)):
        result.match_type = "ontology_family"
        result.relevance_score = min(0.7, 0.45 + 0.08 * len(snippet_hits) + 0.1 * title_overlap)
    elif title_overlap >= 0.25 or snippet_overlap >= 0.18:
        result.match_type = "broad_domain"
        result.relevance_score = min(0.45, 0.2 + title_overlap + snippet_overlap)
    else:
        result.match_type = ""
        result.relevance_score = 0.0
    return result


def _to_web_result(ontology_filename: str, query: str, result: dict) -> WebResult:
    url = _result_url(result)
    title = str(result.get("title") or "").strip()
    return WebResult(
        ontology_filename=ontology_filename,
        search_query=query,
        result_type=classify_result_type(url),
        title=_expand_title_if_needed(title, url),
        url=url,
        snippet=_result_snippet(result),
    )


def deduplicate_results(results: List[WebResult]) -> List[WebResult]:
    seen: Set[str] = set()
    seen_snippets: Set[str] = set()
    seen_snippet_texts: List[str] = []
    deduped: List[WebResult] = []
    for result in results:
        key = _dedup_key(result)
        if not key or key in seen:
            continue
        if _is_excluded_result(result):
            continue
        snippet_key = re.sub(r"\W+", " ", result.snippet.lower()).strip()
        if len(snippet_key) > 80:
            snippet_key = snippet_key[:240]
            if snippet_key in seen_snippets:
                continue
            if any(jaccard_similarity(snippet_key, seen_text) >= 0.72 for seen_text in seen_snippet_texts):
                continue
            seen_snippets.add(snippet_key)
            seen_snippet_texts.append(snippet_key)
        seen.add(key)
        deduped.append(result)
    return deduped


def compute_metrics(filename: str, query: str, results: List[WebResult]) -> WebSearchMetrics:
    domains = {_domain(result.url) for result in results if _domain(result.url)}
    title_overlaps = [jaccard_similarity(query, result.title) for result in results]
    snippet_overlaps = [jaccard_similarity(query, result.snippet) for result in results]

    return WebSearchMetrics(
        ontology_filename=filename,
        search_query=query,
        results_returned=len(results),
        avg_title_keyword_overlap=mean(title_overlaps) if title_overlaps else 0.0,
        avg_snippet_keyword_overlap=mean(snippet_overlaps) if snippet_overlaps else 0.0,
        unique_domains=len(domains),
        github_result_count=sum(1 for result in results if _domain(result.url) == "github.com"),
        academic_result_count=sum(1 for result in results if _is_academic_domain(_domain(result.url))),
    )


def print_metrics_summary(metrics: List[WebSearchMetrics]) -> None:
    total = len(metrics)
    with_results = [metric for metric in metrics if metric.results_returned > 0]
    zero_results = total - len(with_results)

    print("\n" + "=" * 60)
    print("Web Search Metrics Summary")
    print("=" * 60)
    print(f"  Ontologies searched       : {total}")
    print(f"  With >=1 web result       : {len(with_results)}")
    print(f"  With 0 web results        : {zero_results}")

    if metrics:
        print(f"  Mean results / ontology   : {mean(m.results_returned for m in metrics):.1f}")
        print(f"  Mean title overlap        : {mean(m.avg_title_keyword_overlap for m in metrics):.4f}")
        print(f"  Mean snippet overlap      : {mean(m.avg_snippet_keyword_overlap for m in metrics):.4f}")
        print(f"  Mean unique domains       : {mean(m.unique_domains for m in metrics):.1f}")
        print(f"  Total GitHub result sets  : {sum(m.github_result_count for m in metrics)}")
        print(f"  Total academic results    : {sum(m.academic_result_count for m in metrics)}")
    print("=" * 60 + "\n")


def main() -> None:
    _configure_stdout()
    args = parse_args()

    if not METADATA_FILE.exists():
        print(f"ERROR: {METADATA_FILE} not found. Run step1_extract_metadata.py first.")
        return

    ontologies = _load_json(METADATA_FILE, [])
    profiles = _load_json(SEARCH_PROFILE_FILE, {})

    print(f"Loaded {len(ontologies)} ontologies from {METADATA_FILE.name}")
    if profiles:
        print(f"Loaded {len(profiles)} ontology search profiles from {SEARCH_PROFILE_FILE.name}")
    print(f"DuckDuckGo max results/ontology: {args.max_results}")
    print(f"DuckDuckGo queries/ontology: {args.max_queries_per_ontology}")
    print(f"Delay between searches: {WEB_SEARCH_DELAY:g}s\n")

    results_csv = DATA_DIR / "ontology_web_results.csv"
    results_json = DATA_DIR / "ontology_web_results.json"
    metrics_csv = DATA_DIR / "ontology_web_metrics.csv"
    metrics_json = DATA_DIR / "ontology_web_metrics.json"

    all_results: List[WebResult] = []
    all_metrics: List[WebSearchMetrics] = []
    processed: Set[str] = set()

    if not args.fresh:
        all_results = _load_dataclass_rows(results_json, WebResult)
        all_metrics = _load_dataclass_rows(metrics_json, WebSearchMetrics)
        processed = {metric.ontology_filename for metric in all_metrics}
        if processed:
            print(
                f"Resume mode: loaded {len(all_results)} web results and "
                f"{len(all_metrics)} metrics; {len(processed)} ontologies already done."
            )
            print("Use --fresh to discard existing Step 03 outputs and rebuild.\n")

    new_processed = 0

    for index, ontology in enumerate(ontologies, 1):
        filename = ontology.get("filename", "")
        if filename in processed:
            print(f"[{index}/{len(ontologies)}] {filename} - already processed, skipping")
            continue

        if args.max_ontologies is not None and new_processed >= args.max_ontologies:
            print(f"Reached --max-ontologies={args.max_ontologies}; stopping early.")
            break

        profile = _profile_for(ontology, profiles)
        search_queries = derive_web_queries(ontology, profile)
        if args.max_queries_per_ontology > 0:
            search_queries = search_queries[:args.max_queries_per_ontology]
        query = search_queries[0] if search_queries else ""
        if not search_queries:
            print(f"[{index}/{len(ontologies)}] {filename} - no search keywords, skipping")
            metric = WebSearchMetrics(ontology_filename=filename)
            all_metrics.append(metric)
            processed.add(filename)
            new_processed += 1
            _save_quiet(all_results, results_csv, results_json, WebResult)
            _save_quiet(all_metrics, metrics_csv, metrics_json, WebSearchMetrics)
            continue

        print(f"[{index}/{len(ontologies)}] {filename} - {query}")

        scored_results: List[WebResult] = []
        for seed_result in seed_metadata_results(ontology, query):
            seed_result = score_web_result(ontology, profile, query, seed_result)
            if seed_result.match_type == "exact_ontology":
                scored_results.append(seed_result)

        for search_query in search_queries:
            time.sleep(WEB_SEARCH_DELAY)
            try:
                raw_results = search_duckduckgo(search_query, args.max_results)
            except Exception as exc:
                print(f"    DuckDuckGo search failed for {search_query}: {exc}")
                raw_results = []

            for raw_result in raw_results:
                web_result = _to_web_result(filename, search_query, raw_result)
                web_result = score_web_result(ontology, profile, search_query, web_result)
                if web_result.match_type == "exact_ontology":
                    scored_results.append(web_result)

        web_results = deduplicate_results(scored_results)
        web_results.sort(key=lambda result: result.relevance_score, reverse=True)
        web_results = web_results[:args.max_results]

        all_results.extend(web_results)
        metric = compute_metrics(filename, query, web_results)
        all_metrics.append(metric)
        processed.add(filename)
        new_processed += 1

        print(
            f"    -> {len(web_results)} results, "
            f"{metric.unique_domains} unique domains, "
            f"{metric.github_result_count} GitHub domain hits, "
            f"{metric.academic_result_count} academic hits"
        )

        _save_quiet(all_results, results_csv, results_json, WebResult)
        _save_quiet(all_metrics, metrics_csv, metrics_json, WebSearchMetrics)

    print(f"\nTotal: {len(all_results)} web results across {len(all_metrics)} ontologies")
    print_metrics_summary(all_metrics)
    print("Done")


if __name__ == "__main__":
    main()
