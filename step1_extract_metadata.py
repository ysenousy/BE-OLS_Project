"""
step1_extract_metadata.py – Phase 2: TTL Metadata Extraction + Evaluation Metrics

Fetches ~120 TTL ontology files from the CyberbuildLab/BE-OLS GitHub repo,
parses each with rdflib, extracts ontology-level metadata, and computes
per-ontology quality/structural metrics.

Outputs:
  Files are written under the data/ directory.
  data/ontology_metadata.csv / .json       – extracted metadata per ontology
  data/ontology_metadata_metrics.csv / .json – quality & structural metrics
"""

from __future__ import annotations

import time
from pathlib import Path
from statistics import mean
from typing import List, Tuple

import requests
import rdflib
from rdflib import RDF, RDFS, OWL, Namespace

from common import (
    OntologyRow,
    MetadataMetrics,
    save_csv,
    save_json,
)

# ---------------------------------------------------------------------------
# RDF namespace shortcuts
# ---------------------------------------------------------------------------
DCTERMS = Namespace("http://purl.org/dc/terms/")
VANN = Namespace("http://purl.org/vocab/vann/")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")
SCHEMA = Namespace("http://schema.org/")
DC = Namespace("http://purl.org/dc/elements/1.1/")

GITHUB_API_URL = (
    "https://api.github.com/repos/CyberbuildLab/BE-OLS"
    "/contents/data/source/Ontologies_TTL"
)
RAW_BASE = (
    "https://raw.githubusercontent.com/CyberbuildLab/BE-OLS"
    "/main/data/source/Ontologies_TTL"
)

PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"


# ---------------------------------------------------------------------------
# Step 4 – Build TTL file list from GitHub
# ---------------------------------------------------------------------------

def fetch_ttl_file_list() -> List[dict]:
    """Return list of {name, download_url} for every .ttl in the repo folder."""
    print("[Step 4] Fetching TTL file list from GitHub …")
    resp = requests.get(GITHUB_API_URL, timeout=30)
    resp.raise_for_status()
    entries = resp.json()
    ttl_files = [
        {"name": e["name"], "download_url": e.get("download_url") or f"{RAW_BASE}/{e['name']}"}
        for e in entries
        if e["name"].lower().endswith(".ttl")
    ]
    print(f"  Found {len(ttl_files)} TTL files")
    return ttl_files


# ---------------------------------------------------------------------------
# Step 5 – Download & parse a single TTL file
# ---------------------------------------------------------------------------

def _first_literal(graph: rdflib.Graph, subject, predicate, lang_pref: str = "en") -> str:
    """Get the first literal value for (subject, predicate), preferring *lang_pref*."""
    best = ""
    for obj in graph.objects(subject, predicate):
        val = str(obj).strip()
        if not val:
            continue
        if hasattr(obj, "language") and obj.language == lang_pref:
            return val
        if not best:
            best = val
    return best


def _all_uri_objects(graph: rdflib.Graph, subject, predicate) -> List[str]:
    """Collect all URI object values for (subject, predicate)."""
    return [str(o) for o in graph.objects(subject, predicate) if isinstance(o, rdflib.URIRef)]


def _local_name(uri: str) -> str:
    """Extract the local/fragment part of a URI."""
    if "#" in uri:
        return uri.rsplit("#", 1)[-1]
    return uri.rsplit("/", 1)[-1]


def _find_ontology_subject(graph: rdflib.Graph):
    """Find the main owl:Ontology subject in the graph."""
    for s in graph.subjects(RDF.type, OWL.Ontology):
        return s
    return None


def _extract_creators(graph: rdflib.Graph, ont_subject) -> str:
    """Extract creator names from dcterms:creator / dc:creator."""
    names: List[str] = []
    for pred in (DCTERMS.creator, DC.creator):
        for obj in graph.objects(ont_subject, pred):
            if isinstance(obj, rdflib.Literal):
                names.append(str(obj).strip())
            elif isinstance(obj, rdflib.URIRef):
                # try vcard:fn or schema:name
                name = _first_literal(graph, obj, VCARD.fn) or _first_literal(graph, obj, SCHEMA.name)
                if name:
                    names.append(name)
                else:
                    names.append(_local_name(str(obj)))
    return ", ".join(dict.fromkeys(names))  # deduplicate, preserve order


def parse_ttl(filename: str, url: str) -> Tuple[OntologyRow, MetadataMetrics]:
    """Download and parse one TTL file; return (OntologyRow, MetadataMetrics)."""
    metrics = MetadataMetrics(filename=filename)
    row = OntologyRow(filename=filename)

    # Download raw content
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        raw_ttl = resp.text
    except Exception as exc:
        metrics.parse_success = False
        metrics.parse_error = f"Download failed: {exc}"
        return row, metrics

    # Parse with rdflib
    g = rdflib.Graph()
    try:
        g.parse(data=raw_ttl, format="turtle")
    except Exception as exc:
        metrics.parse_success = False
        metrics.parse_error = f"Parse failed: {exc}"
        return row, metrics

    ont = _find_ontology_subject(g)

    # --- Extract ontology-level metadata ---
    if ont is not None:
        row.title = (
            _first_literal(g, ont, DCTERMS.title)
            or _first_literal(g, ont, DC.title)
            or _first_literal(g, ont, RDFS.label)
        )
        row.prefix = _first_literal(g, ont, VANN.preferredNamespacePrefix)
        row.namespace_uri = (
            _first_literal(g, ont, VANN.preferredNamespaceUri)
            or str(ont)
        )
        row.description = (
            _first_literal(g, ont, DCTERMS.description)
            or _first_literal(g, ont, DC.description)
            or _first_literal(g, ont, RDFS.comment)
        )
        row.version = (
            _first_literal(g, ont, OWL.versionInfo)
            or _first_literal(g, ont, DCTERMS.hasVersion)
        )
        row.license = (
            _first_literal(g, ont, DCTERMS.license)
            or ", ".join(_all_uri_objects(g, ont, DCTERMS.license))
        )
        row.creators = _extract_creators(g, ont)
        row.date_modified = _first_literal(g, ont, DCTERMS.modified)
        row.date_issued = _first_literal(g, ont, DCTERMS.issued)
        row.see_also = ", ".join(_all_uri_objects(g, ont, RDFS.seeAlso))
        row.imports = ", ".join(_all_uri_objects(g, ont, OWL.imports))

    # --- Extract classes & properties ---
    class_uris = set(g.subjects(RDF.type, OWL.Class))
    prop_uris = (
        set(g.subjects(RDF.type, OWL.ObjectProperty))
        | set(g.subjects(RDF.type, OWL.DatatypeProperty))
    )
    row.classes = ", ".join(sorted({_local_name(str(c)) for c in class_uris if isinstance(c, rdflib.URIRef)}))
    row.properties = ", ".join(sorted({_local_name(str(p)) for p in prop_uris if isinstance(p, rdflib.URIRef)}))

    # --- Compute metrics (Step 7b) ---
    metrics.axiom_count = len(g)
    metrics.class_count = len(class_uris)
    metrics.property_count = len(prop_uris)
    metrics.import_count = len(_all_uri_objects(g, ont, OWL.imports)) if ont else 0
    metrics.has_title = bool(row.title)
    metrics.has_namespace_uri = bool(row.namespace_uri)
    metrics.has_description = bool(row.description)
    metrics.has_license = bool(row.license)
    metrics.has_version = bool(row.version)
    metrics.has_creators = bool(row.creators)

    # Completeness – count non-empty OntologyRow fields (excluding 'filename')
    # Generated search keywords and the always-present filename are excluded.
    data_fields = [
        row.title, row.prefix, row.namespace_uri, row.description,
        row.version, row.license, row.creators, row.date_modified,
        row.date_issued, row.see_also, row.imports, row.classes,
        row.properties,
    ]
    metrics.fields_filled = sum(1 for f in data_fields if f)
    metrics.completeness_pct = round(metrics.fields_filled / metrics.fields_total * 100, 1)

    # Annotation / comment coverage
    all_entities = class_uris | prop_uris
    if all_entities:
        has_label = sum(1 for e in all_entities if any(g.objects(e, RDFS.label)))
        has_comment = sum(1 for e in all_entities if any(g.objects(e, RDFS.comment)))
        metrics.annotation_coverage_pct = round(has_label / len(all_entities) * 100, 1)
        metrics.comment_coverage_pct = round(has_comment / len(all_entities) * 100, 1)

    # Language count
    lang_tags: set = set()
    for pred in (RDFS.label, RDFS.comment):
        for _, _, obj in g.triples((None, pred, None)):
            if hasattr(obj, "language") and obj.language:
                lang_tags.add(obj.language)
    metrics.language_count = len(lang_tags)

    return row, metrics


# ---------------------------------------------------------------------------
# Step 6 – Derive search keywords
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Step 7b – Print summary
# ---------------------------------------------------------------------------

def print_metrics_summary(metrics_list: List[MetadataMetrics]) -> None:
    """Print an overview of metadata quality across all ontologies."""
    total = len(metrics_list)
    parsed = [m for m in metrics_list if m.parse_success]
    failed = [m for m in metrics_list if not m.parse_success]

    print("\n" + "=" * 60)
    print("  METADATA EVALUATION SUMMARY")
    print("=" * 60)
    print(f"  Total ontologies      : {total}")
    print(f"  Parsed successfully   : {len(parsed)}")
    print(f"  Parse failures        : {len(failed)}")

    if parsed:
        avg_comp = mean(m.completeness_pct for m in parsed)
        print(f"  Mean completeness %   : {avg_comp:.1f}%")

        has_title = sum(1 for m in parsed if m.has_title)
        # More precise counts from rows – use metrics booleans
        has_desc = sum(1 for m in parsed if m.has_description)
        has_ns = sum(1 for m in parsed if m.has_namespace_uri)
        has_lic = sum(1 for m in parsed if m.has_license)
        has_ver = sum(1 for m in parsed if m.has_version)
        has_cre = sum(1 for m in parsed if m.has_creators)
        print(f"  With title            : {has_title}/{len(parsed)}")
        print(f"  With namespace URI    : {has_ns}/{len(parsed)}")
        print(f"  With description      : {has_desc}/{len(parsed)}")
        print(f"  With license          : {has_lic}/{len(parsed)}")
        print(f"  With version          : {has_ver}/{len(parsed)}")
        print(f"  With creators         : {has_cre}/{len(parsed)}")

        avg_cls = mean(m.class_count for m in parsed)
        avg_prop = mean(m.property_count for m in parsed)
        avg_ax = mean(m.axiom_count for m in parsed)
        print(f"  Mean class count      : {avg_cls:.1f}")
        print(f"  Mean property count   : {avg_prop:.1f}")
        print(f"  Mean axiom count      : {avg_ax:.1f}")

        non_zero_ann = [m for m in parsed if m.class_count + m.property_count > 0]
        if non_zero_ann:
            avg_ann = mean(m.annotation_coverage_pct for m in non_zero_ann)
            avg_cmt = mean(m.comment_coverage_pct for m in non_zero_ann)
            print(f"  Mean annotation cov.  : {avg_ann:.1f}%")
            print(f"  Mean comment cov.     : {avg_cmt:.1f}%")

    if failed:
        print("\n  Failed files:")
        for m in failed:
            print(f"    - {m.filename}: {m.parse_error}")

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ttl_files = fetch_ttl_file_list()

    rows: List[OntologyRow] = []
    metrics_list: List[MetadataMetrics] = []

    for i, entry in enumerate(ttl_files, 1):
        name = entry["name"]
        url = entry["download_url"]
        print(f"[Step 5] ({i}/{len(ttl_files)}) Parsing {name} …")

        row, met = parse_ttl(name, url)
        rows.append(row)
        metrics_list.append(met)

        # Be polite to GitHub's CDN
        if i % 20 == 0:
            time.sleep(1)

    # Step 7 – save metadata
    print("\n[Step 7] Saving ontology metadata …")
    save_csv(rows, DATA_DIR / "ontology_metadata.csv")
    save_json(rows, DATA_DIR / "ontology_metadata.json")

    # Step 7b – save & print metrics
    print("[Step 7b] Saving metadata metrics …")
    save_csv(metrics_list, DATA_DIR / "ontology_metadata_metrics.csv")
    save_json(metrics_list, DATA_DIR / "ontology_metadata_metrics.json")
    print_metrics_summary(metrics_list)

    print("Done")


if __name__ == "__main__":
    main()
