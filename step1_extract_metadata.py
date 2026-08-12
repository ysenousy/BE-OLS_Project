"""
step1_extract_metadata.py – Phase 2: Ontology Metadata (from curated repo JSON)

The CURATED metadata maintained by the CyberbuildLab/BE-OLS repository in
``data/Ontologies_forRepo.json`` (auto-generated from the collaborators'
``Ontologies.xlsx`` workbook) is the single source of truth for this step.
Every curated record becomes one ontology row.

Earlier versions enumerated the repo's ``Ontologies_TTL`` folder and looked up
a curated record per filename. That made the TTL folder the real source: any
curated ontology without a TTL file was invisible to the whole pipeline, which
dropped 26 of the 146 curated records. It also allowed silent mis-joins - a
filename could match the wrong record and inherit its title, namespace and
description - so the matching layer is gone entirely.

Ontologies are keyed by curated ``Prefix``, which is unique across all records
and never blank. ``URI`` is unsuitable as a key: 31 records leave it empty.

Note: ``MetadataMetrics.parse_success`` no longer denotes TTL parsing (nothing
is parsed here). It records whether a curated record yielded a usable row, and
is retained because step 4 reads it.

Outputs (unchanged, written under the data/ directory):
  data/ontology_metadata.csv / .json          – extracted metadata per ontology
  data/ontology_metadata_metrics.csv / .json  – quality & structural metrics
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional

import requests

from common import (
    OntologyRow,
    MetadataMetrics,
    save_csv,
    save_json,
)

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
# Curated metadata maintained in the repo (generated from Ontologies.xlsx).
REPO_METADATA_URL = (
    "https://raw.githubusercontent.com/CyberbuildLab/BE-OLS"
    "/main/data/Ontologies_forRepo.json"
)

PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"


# ---------------------------------------------------------------------------
# Fetch curated metadata
# ---------------------------------------------------------------------------

def fetch_repo_metadata() -> List[dict]:
    """Return the curated ontology records from ``Ontologies_forRepo.json``."""
    print("[Step 1] Fetching curated metadata (Ontologies_forRepo.json) …")
    resp = requests.get(REPO_METADATA_URL, timeout=60)
    resp.raise_for_status()
    records = resp.json()
    print(f"  Loaded {len(records)} curated records")
    return records


def ontology_key(rec: dict) -> str:
    """Return the identifier for a curated record: its Prefix.

    Prefixes are unique across the curated set and always present, so no
    normalisation beyond trimming is applied - the curated value is used
    verbatim as the ontology's identity throughout the pipeline.
    """
    return _text(rec.get("Prefix")).strip(":")


def check_keys(records: List[dict]) -> List[str]:
    """Return a list of key problems, so a bad join fails loudly rather than silently."""
    problems: List[str] = []
    seen: Dict[str, int] = {}
    for position, rec in enumerate(records, 1):
        key = ontology_key(rec)
        if not key:
            problems.append(
                f"record {position} ({_text(rec.get('Title')) or 'untitled'}) has no Prefix"
            )
            continue
        if key in seen:
            problems.append(
                f"duplicate Prefix '{key}' at records {seen[key]} and {position}"
            )
        else:
            seen[key] = position
    return problems


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


def record_to_row(key: str, rec: dict) -> OntologyRow:
    """Map a curated record onto the OntologyRow schema."""
    return OntologyRow(
        filename=key,
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


def record_to_metrics(key: str, rec: Optional[dict], row: OntologyRow) -> MetadataMetrics:
    """Map a curated record onto the MetadataMetrics schema."""
    metrics = MetadataMetrics(filename=key)

    if rec is None:
        metrics.parse_success = False
        metrics.parse_error = "Curated record could not be mapped"
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
    mapped = [m for m in metrics_list if m.parse_success]
    unmapped = [m for m in metrics_list if not m.parse_success]

    print("\n" + "=" * 60)
    print("  METADATA EVALUATION SUMMARY (curated repo source)")
    print("=" * 60)
    print(f"  Curated records       : {total}")
    print(f"  Mapped to a row       : {len(mapped)}")
    print(f"  Unmapped              : {len(unmapped)}")

    if mapped:
        avg_comp = mean(m.completeness_pct for m in mapped)
        print(f"  Mean completeness %   : {avg_comp:.1f}%")

        print(f"  With title            : {sum(m.has_title for m in mapped)}/{len(mapped)}")
        print(f"  With namespace URI    : {sum(m.has_namespace_uri for m in mapped)}/{len(mapped)}")
        print(f"  With description      : {sum(m.has_description for m in mapped)}/{len(mapped)}")
        print(f"  With license          : {sum(m.has_license for m in mapped)}/{len(mapped)}")
        print(f"  With version          : {sum(m.has_version for m in mapped)}/{len(mapped)}")
        print(f"  With creators         : {sum(m.has_creators for m in mapped)}/{len(mapped)}")

        print(f"  Mean class count      : {mean(m.class_count for m in mapped):.1f}")
        print(f"  Mean property count   : {mean(m.property_count for m in mapped):.1f}")
        print(f"  Mean annotation score : {mean(m.annotation_score for m in mapped):.1f}")
        print(f"  Mean FOOPS score      : {mean(m.foops_score for m in mapped):.2f}")
        print(f"  Mean quality score    : {mean(m.quality_score for m in mapped):.2f}")

    if unmapped:
        print("\n  Unmapped records:")
        for m in unmapped:
            print(f"    - {m.filename}")

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    records = fetch_repo_metadata()

    problems = check_keys(records)
    if problems:
        print("\n[Step 2] Curated key problems - refusing to build a corrupt dataset:")
        for problem in problems:
            print(f"    - {problem}")
        raise SystemExit(
            "Curated Prefix values must be unique and non-empty; fix the source first."
        )
    print(f"[Step 2] Key check passed: {len(records)} unique, non-empty prefixes")

    rows: List[OntologyRow] = []
    metrics_list: List[MetadataMetrics] = []

    for i, rec in enumerate(records, 1):
        key = ontology_key(rec)
        print(f"[Step 3] ({i}/{len(records)}) {key}")
        row = record_to_row(key, rec)
        metrics_list.append(record_to_metrics(key, rec, row))
        rows.append(row)

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
