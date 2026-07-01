"""
step1_extract_metadata.py – Phase 2: Ontology Metadata (from curated repo JSON)

Instead of downloading ~120 TTL files and parsing each with rdflib, this step
reads the CURATED metadata the CyberbuildLab/BE-OLS repository already maintains
in ``data/Ontologies_forRepo.json`` (auto-generated from the collaborators'
``Ontologies.xlsx`` workbook). That file provides richer, human-curated fields
(domains, publisher, quality/FOOPS/alignment scores, class & property counts)
and avoids TTL parse failures entirely.

To keep the rest of the pipeline (steps 2–4) unchanged, each curated record is
matched to its ``.ttl`` filename in the repo's ``Ontologies_TTL`` folder and
mapped onto the existing OntologyRow / MetadataMetrics schema keyed by filename.

Outputs (unchanged, written under the data/ directory):
  data/ontology_metadata.csv / .json          – extracted metadata per ontology
  data/ontology_metadata_metrics.csv / .json  – quality & structural metrics
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

import requests

from common import (
    OntologyRow,
    MetadataMetrics,
    save_csv,
    save_json,
)

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
GITHUB_API_URL = (
    "https://api.github.com/repos/CyberbuildLab/BE-OLS"
    "/contents/data/source/Ontologies_TTL"
)
RAW_BASE = (
    "https://raw.githubusercontent.com/CyberbuildLab/BE-OLS"
    "/main/data/source/Ontologies_TTL"
)
# Curated metadata maintained in the repo (generated from Ontologies.xlsx).
REPO_METADATA_URL = (
    "https://raw.githubusercontent.com/CyberbuildLab/BE-OLS"
    "/main/data/Ontologies_forRepo.json"
)

PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"

# Explicit filename-stem -> curated-record Prefix aliases for the handful of
# TTL files whose name does not equal (or normalise to) the curated Prefix.
STEM_PREFIX_ALIASES = {
    "geosparql": "geo",
    "asbingowl": "asb",
    "eco": "ec",
    "ifc4-add2": "ifc",
    "lca-c-reno": "lca-c-renovation",
    "mdo": "core",
}


# ---------------------------------------------------------------------------
# Fetch the authoritative TTL filename list (names only – no download/parse)
# ---------------------------------------------------------------------------

def fetch_ttl_file_list() -> List[str]:
    """Return the list of ``.ttl`` filenames in the repo's Ontologies_TTL folder."""
    print("[Step 1] Fetching TTL filename list from GitHub …")
    resp = requests.get(GITHUB_API_URL, timeout=30)
    resp.raise_for_status()
    entries = resp.json()
    names = sorted(e["name"] for e in entries if e["name"].lower().endswith(".ttl"))
    print(f"  Found {len(names)} TTL files")
    return names


# ---------------------------------------------------------------------------
# Fetch curated metadata and build a lookup index
# ---------------------------------------------------------------------------

def fetch_repo_metadata() -> List[dict]:
    """Return the curated ontology records from ``Ontologies_forRepo.json``."""
    print("[Step 2] Fetching curated metadata (Ontologies_forRepo.json) …")
    resp = requests.get(REPO_METADATA_URL, timeout=60)
    resp.raise_for_status()
    records = resp.json()
    print(f"  Loaded {len(records)} curated records")
    return records


def _norm(value: Optional[str]) -> str:
    """Lowercase and strip everything but ASCII letters/digits."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def build_record_index(records: List[dict]) -> Dict[str, dict]:
    """Index curated records under several normalised keys for robust matching.

    Keys added per record: normalised Prefix, a SAREF alias (``s4x`` -> ``saref4x``),
    normalised Title, and the normalised last path segment of the URI.
    """
    index: Dict[str, dict] = {}

    def add(key: str, rec: dict) -> None:
        key = _norm(key)
        # First writer wins so a specific Prefix isn't overwritten by a Title clash.
        if key and key not in index:
            index[key] = rec

    for rec in records:
        prefix = (rec.get("Prefix") or "").strip().strip(":")
        add(prefix, rec)
        if prefix.lower().startswith("s4"):
            add("saref4" + prefix[2:], rec)  # s4bldg -> saref4bldg
        add(rec.get("Title"), rec)
        uri = (rec.get("URI") or "").rstrip("/#")
        if uri:
            add(uri.rsplit("/", 1)[-1], rec)
    return index


def match_record(filename: str, index: Dict[str, dict]) -> Optional[dict]:
    """Find the curated record for a ``.ttl`` filename, or None."""
    stem = filename.rsplit(".", 1)[0]
    for candidate in (STEM_PREFIX_ALIASES.get(stem.lower(), ""), stem):
        rec = index.get(_norm(candidate))
        if rec:
            return rec
    return None


# ---------------------------------------------------------------------------
# Field-mapping helpers
# ---------------------------------------------------------------------------

def _text(value) -> str:
    """Coerce a JSON value to a clean display string (None/NaN -> '')."""
    if value is None:
        return ""
    return str(value).strip()


def _parse_people(value) -> str:
    """Turn a Creator/Publisher value into a comma-separated string.

    Curated values are sometimes a Python-list-literal string such as
    ``"['https://…/edlira', 'https://…/pan']"``.
    """
    text = _text(value)
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        try:
            items = ast.literal_eval(text)
            if isinstance(items, (list, tuple)):
                return ", ".join(_text(i) for i in items if _text(i))
        except (ValueError, SyntaxError):
            pass
    return text


def _num(value) -> float:
    """Best-effort float from a curated numeric value; 0.0 on failure/blank."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if num != num else num  # guard against NaN


def _joined(*values) -> str:
    """Join several comma-listy fields into one deduped comma string."""
    parts: List[str] = []
    for value in values:
        for token in re.split(r"[,;]", _text(value)):
            token = token.strip()
            if token and token.lower() != "none":
                parts.append(token)
    return ", ".join(dict.fromkeys(parts))


def record_to_row(filename: str, rec: dict) -> OntologyRow:
    """Map a curated record onto the OntologyRow schema."""
    return OntologyRow(
        filename=filename,
        title=_text(rec.get("Title")),
        prefix=_text(rec.get("Prefix")).strip(":"),
        namespace_uri=_text(rec.get("URI")),
        description=_text(rec.get("Description")),
        version=_text(rec.get("Version")),
        license=_text(rec.get("License")),
        creators=_joined(_parse_people(rec.get("Creator")), _parse_people(rec.get("Publisher"))),
        date_modified="",  # not tracked in curated metadata
        date_issued=_text(rec.get("Created")),
        see_also=_joined(rec.get("Reference Source")),
        imports=_joined(rec.get("Linked-to Upper Ontologies"), rec.get("Linked-to AECO Ontologies")),
        classes="",     # curated source provides counts, not class names
        properties="",   # curated source provides counts, not property names
        primary_domain=_text(rec.get("Primary Domain")),
        secondary_domain=_text(rec.get("Secondary Domain")),
        cluster=_text(rec.get("Cluster")),
        conforms_to=_joined(rec.get("Conforms to Standard(s)")),
    )


def record_to_metrics(filename: str, rec: Optional[dict], row: OntologyRow) -> MetadataMetrics:
    """Map a curated record onto the MetadataMetrics schema."""
    metrics = MetadataMetrics(filename=filename)

    if rec is None:
        metrics.parse_success = False
        metrics.parse_error = "No curated metadata record matched this TTL filename"
        return metrics

    data_props = int(_num(rec.get("Number of Data Properties")))
    obj_props = int(_num(rec.get("Number of Object Properties")))

    metrics.class_count = int(_num(rec.get("Number of Classes")))
    metrics.data_property_count = data_props
    metrics.object_property_count = obj_props
    metrics.property_count = data_props + obj_props
    metrics.axiom_count = 0  # not available from curated metadata
    metrics.import_count = len([t for t in row.imports.split(",") if t.strip()])
    metrics.language_count = 0  # not available from curated metadata

    # Repo-native quality scores
    metrics.annotation_score = _num(rec.get("Annotation Score"))
    metrics.annotation_coverage_pct = metrics.annotation_score  # already a 0–100 %
    metrics.comment_coverage_pct = 0.0  # not separately available
    metrics.foops_score = _num(rec.get("FOOPs Score"))
    metrics.alignment_score = _num(rec.get("Alignment Score"))
    metrics.accessibility_score = _num(rec.get("Accessibility Score"))
    metrics.quality_score = _num(rec.get("Quality Score"))
    metrics.has_documentation = bool(_num(rec.get("Has Documentation")))
    metrics.has_serialization = bool(_num(rec.get("Has Serialization")))
    metrics.has_conceptual_model = bool(_num(rec.get("Has Conceptual Model")))

    metrics.has_title = bool(row.title)
    metrics.has_namespace_uri = bool(row.namespace_uri)
    metrics.has_description = bool(row.description)
    metrics.has_license = bool(row.license)
    metrics.has_version = bool(row.version)
    metrics.has_creators = bool(row.creators)

    # Completeness – count non-empty OntologyRow data fields (excluding 'filename').
    data_fields = [
        row.title, row.prefix, row.namespace_uri, row.description,
        row.version, row.license, row.creators, row.date_modified,
        row.date_issued, row.see_also, row.imports, row.classes,
        row.properties,
    ]
    metrics.fields_filled = sum(1 for f in data_fields if f)
    metrics.completeness_pct = round(metrics.fields_filled / metrics.fields_total * 100, 1)

    return metrics


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_metrics_summary(metrics_list: List[MetadataMetrics]) -> None:
    """Print an overview of metadata quality across all ontologies."""
    total = len(metrics_list)
    matched = [m for m in metrics_list if m.parse_success]
    unmatched = [m for m in metrics_list if not m.parse_success]

    print("\n" + "=" * 60)
    print("  METADATA EVALUATION SUMMARY (curated repo source)")
    print("=" * 60)
    print(f"  Total ontologies      : {total}")
    print(f"  Matched to curated rec: {len(matched)}")
    print(f"  Unmatched             : {len(unmatched)}")

    if matched:
        avg_comp = mean(m.completeness_pct for m in matched)
        print(f"  Mean completeness %   : {avg_comp:.1f}%")

        print(f"  With title            : {sum(m.has_title for m in matched)}/{len(matched)}")
        print(f"  With namespace URI    : {sum(m.has_namespace_uri for m in matched)}/{len(matched)}")
        print(f"  With description      : {sum(m.has_description for m in matched)}/{len(matched)}")
        print(f"  With license          : {sum(m.has_license for m in matched)}/{len(matched)}")
        print(f"  With version          : {sum(m.has_version for m in matched)}/{len(matched)}")
        print(f"  With creators         : {sum(m.has_creators for m in matched)}/{len(matched)}")

        print(f"  Mean class count      : {mean(m.class_count for m in matched):.1f}")
        print(f"  Mean property count   : {mean(m.property_count for m in matched):.1f}")
        print(f"  Mean annotation score : {mean(m.annotation_score for m in matched):.1f}")
        print(f"  Mean FOOPS score      : {mean(m.foops_score for m in matched):.2f}")
        print(f"  Mean quality score    : {mean(m.quality_score for m in matched):.2f}")

    if unmatched:
        print("\n  Unmatched files (no curated record):")
        for m in unmatched:
            print(f"    - {m.filename}")

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ttl_files = fetch_ttl_file_list()
    records = fetch_repo_metadata()
    index = build_record_index(records)

    rows: List[OntologyRow] = []
    metrics_list: List[MetadataMetrics] = []

    for i, name in enumerate(ttl_files, 1):
        rec = match_record(name, index)
        status = "matched" if rec else "NO MATCH"
        print(f"[Step 3] ({i}/{len(ttl_files)}) {name} … {status}")

        row = record_to_row(name, rec) if rec else OntologyRow(filename=name)
        met = record_to_metrics(name, rec, row)
        rows.append(row)
        metrics_list.append(met)

    print("\n[Step 4] Saving ontology metadata …")
    save_csv(rows, DATA_DIR / "ontology_metadata.csv")
    save_json(rows, DATA_DIR / "ontology_metadata.json")

    print("[Step 4b] Saving metadata metrics …")
    save_csv(metrics_list, DATA_DIR / "ontology_metadata_metrics.csv")
    save_json(metrics_list, DATA_DIR / "ontology_metadata_metrics.json")
    print_metrics_summary(metrics_list)

    print("Done")


if __name__ == "__main__":
    main()
