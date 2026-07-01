"""
generate_search_profiles.py – Regenerate ontology search profiles from metadata

Builds ``data/ontology_search_profiles.json`` deterministically from the curated
metadata produced by ``step1_extract_metadata.py`` (``ontology_metadata.json``).
This replaces the previously hand-maintained profile file so the per-ontology
search keywords stay in sync with the metadata source and its curated domain
signals (primary/secondary domain, cluster, standards, linked ontologies).

Profile schema (one entry per TTL filename), consumed by steps 2 & 3:
  queries            – ordered search-query strings (queries[0] is the primary)
  required_terms     – strong identity anchors (title, acronym, namespace, standards)
  optional_terms     – weaker domain/description/linked-ontology context terms
  exclude_terms      – biomedical/off-domain terms that veto a paper match
  known_dois         – DOIs harvested from the ontology's see_also links
  generic_name       – True when the title carries no distinctive terms
  required_terms_all – terms that must ALL be present (kept empty = permissive)

Run after step 1:
    python generate_search_profiles.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List

PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"
METADATA_FILE = DATA_DIR / "ontology_metadata.json"
PROFILE_FILE = DATA_DIR / "ontology_search_profiles.json"

# Fixed biomedical / off-domain veto list (preserved from the prior profile file).
STD_EXCLUDE = [
    "anatomy", "apoptosis", "bioinformatics", "biology", "biomedical", "cancer",
    "clinical", "disease", "drug", "gene", "genomic", "medicine", "neuroscience",
    "psychology", "tourism", "cognitive science", "gene expression", "genetics",
    "health care", "hospital", "human health", "lymphocyte", "nucleic acid",
    "philosophy", "protein", "single-cell", "stem cell", "systems biology",
]

# Words too generic to serve as distinctive optional/identity terms.
STOP_WORDS = {
    "about", "agent", "agents", "annotation", "annotations", "application", "area",
    "areas", "assessment", "based", "building", "buildings", "built", "core", "data",
    "design", "digital", "domain", "element", "elements", "environment", "extension",
    "generic", "information", "interest", "interests", "management", "model", "models",
    "object", "objects", "ontology", "ontologies", "process", "processes", "product",
    "products", "project", "quality", "related", "resource", "resources", "semantic",
    "smart", "standard", "standards", "system", "systems", "the", "with",
    "com", "net", "org", "http", "https", "www", "w3id", "purl", "lbd", "def",
}

# Generic metadata/upper vocabularies – not useful as ontology-specific search terms.
GENERIC_VOCAB = {
    "dcterms", "dct", "dc", "vann", "voaf", "vcard", "skos", "foaf", "owl", "rdf",
    "rdfs", "prov", "schema", "schema1", "ns1", "cc", "xsd", "sh", "shacl", "dcat",
    "time", "geo", "geosparql", "sosa", "ssn", "org",
}

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_ACRONYM_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9]{1,8})\)")


def _dedupe(values: Iterable[str]) -> List[str]:
    """Case-insensitive de-duplication that preserves first-seen display form."""
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        value = (value or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _clean_ns(namespace: str) -> str:
    return (namespace or "").strip().rstrip("/#")


def _significant_words(*texts: str) -> List[str]:
    """Distinctive single words (len >= 4) drawn from the given texts, in order."""
    words: List[str] = []
    for text in texts:
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", text or ""):
            low = token.lower()
            if low not in STOP_WORDS:
                words.append(low)
    return _dedupe(words)


def _significant_phrases(text: str, max_phrases: int = 4) -> List[str]:
    """Distinctive adjacent-word phrases (both words len >= 4, non-stop).

    Two-word concept phrases (e.g. "demand response", "circular economy") count
    as *strong* profile terms, which is what drives family/reuse classification.
    """
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", text or "")
    phrases: List[str] = []
    for first, second in zip(tokens, tokens[1:]):
        low_a, low_b = first.lower(), second.lower()
        if low_a in STOP_WORDS or low_b in STOP_WORDS:
            continue
        phrases.append(f"{low_a} {low_b}")
    return _dedupe(phrases)[:max_phrases]


def _split_list(value: str) -> List[str]:
    """Split a comma/semicolon-separated metadata field into clean tokens."""
    parts = [p.strip() for p in re.split(r"[,;]", value or "")]
    return [p for p in parts if p and p.lower() != "none"]


def build_profile(row: dict) -> dict:
    title = (row.get("title") or "").strip()
    prefix = (row.get("prefix") or "").strip()
    namespace = _clean_ns(row.get("namespace_uri") or "")
    description = (row.get("description") or "").strip()
    primary = (row.get("primary_domain") or "").strip()
    secondary = (row.get("secondary_domain") or "").strip()
    cluster = (row.get("cluster") or "").strip()
    conforms = row.get("conforms_to") or ""
    imports = row.get("imports") or ""
    see_also = row.get("see_also") or ""

    acronym_match = _ACRONYM_RE.search(title)
    acronym = acronym_match.group(1) if acronym_match else ""
    standards = _split_list(conforms)
    linked = [t for t in _split_list(imports) if len(t) >= 3 and t.lower() not in GENERIC_VOCAB]
    title_words = _significant_words(title)

    # --- queries (most identifying first) ---
    queries: List[str] = []
    if title:
        queries.append(f'"{title}" ontology')
    if acronym and acronym.lower() != title.lower():
        queries.append(f'"{acronym}" ontology')
    if title and primary:
        queries.append(f'"{title}" {primary} ontology')
    if namespace:
        queries.append(f'"{namespace}"')
    if prefix and len(prefix) >= 4:
        queries.append(f'"{prefix}" ontology')

    # --- required_terms: strong identity anchors + explicit standards ---
    required: List[str] = []
    if len(title) >= 4:
        required.append(title)
    if acronym:
        required.append(acronym)
    if namespace:
        required.append(namespace)
        if namespace.startswith("https://"):
            required.append(namespace.replace("https://", "http://", 1))
        elif namespace.startswith("http://"):
            required.append(namespace.replace("http://", "https://", 1))
    required.extend(standards)

    # --- optional_terms: domain / description / linked-ontology context ---
    # Full curated domain phrases and description bigrams are kept intact so that
    # multi-word concepts stay "strong" and can drive family/reuse evidence;
    # single words are added afterwards as weak context.
    optional: List[str] = [primary, secondary, cluster]
    optional.extend(_significant_phrases(description, 4))
    optional.extend(title_words)
    optional.extend(_significant_words(primary, secondary, cluster))
    optional.extend(_significant_words(description)[:8])
    optional.extend(linked[:6])
    if prefix and len(prefix) >= 4:
        optional.append(prefix)
    # Don't repeat anything already used as a required (identity) term.
    required_lower = {t.lower() for t in required}
    optional = [t for t in _dedupe(optional) if t.lower() not in required_lower]

    known_dois = _dedupe(m.group(0).rstrip(".") for m in _DOI_RE.finditer(see_also))

    return {
        "queries": _dedupe(queries),
        "required_terms": _dedupe(required),
        "optional_terms": optional,
        "exclude_terms": list(STD_EXCLUDE),
        "known_dois": known_dois,
        "generic_name": len(title_words) == 0,
        "required_terms_all": [],
    }


def main() -> None:
    with open(METADATA_FILE, encoding="utf-8") as fh:
        rows = json.load(fh)

    profiles: Dict[str, dict] = {}
    for row in rows:
        filename = (row.get("filename") or "").strip()
        if filename:
            profiles[filename] = build_profile(row)

    with open(PROFILE_FILE, "w", encoding="utf-8") as fh:
        json.dump(profiles, fh, indent=1, ensure_ascii=False)

    generic = sum(1 for p in profiles.values() if p["generic_name"])
    with_dois = sum(1 for p in profiles.values() if p["known_dois"])
    avg_req = sum(len(p["required_terms"]) for p in profiles.values()) / max(len(profiles), 1)
    avg_opt = sum(len(p["optional_terms"]) for p in profiles.values()) / max(len(profiles), 1)
    print(f"Wrote {len(profiles)} profiles -> {PROFILE_FILE}")
    print(f"  generic_name=True     : {generic}")
    print(f"  with known_dois       : {with_dois}")
    print(f"  mean required_terms   : {avg_req:.1f}")
    print(f"  mean optional_terms   : {avg_opt:.1f}")


if __name__ == "__main__":
    main()
