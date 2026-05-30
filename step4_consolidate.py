"""
step4_consolidate.py - Phase 5: Dataset Consolidation

Merges ontology metadata, academic paper results, web results, and per-step
metrics into a compact dataset plus an aggregate evaluation summary.

Inputs:
  data/ontology_metadata.json
  data/ontology_metadata_metrics.json
  data/ontology_papers.json
  data/ontology_papers_metrics.json
  data/ontology_web_results.json
  data/ontology_web_metrics.json

Outputs:
  data/be_ols_dataset.csv / .json
  data/evaluation_summary.json
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Iterable, Sequence

from common import ConsolidatedOntologyRow, save_csv, save_json


PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,]+", re.IGNORECASE)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def join_values(values: Iterable[str], limit: int = 5) -> str:
    cleaned = []
    seen = set()
    for value in values:
        value = " ".join(str(value or "").split())
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return " | ".join(cleaned)


def count_by(rows: Sequence[dict], key: str) -> dict:
    counts = Counter(str(row.get(key, "") or "") for row in rows if row.get(key))
    return dict(sorted(counts.items()))


def rows_by_filename(rows: Sequence[dict], filename_key: str) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get(filename_key, "")].append(row)
    return grouped


def pct(numerator: int, denominator: int) -> float:
    return round((numerator / denominator * 100), 1) if denominator else 0.0


def build_dataset(
    metadata_rows: list[dict],
    metadata_metrics: dict,
    papers_by_ontology: dict,
    paper_metrics: dict,
    web_by_ontology: dict,
    web_metrics: dict,
) -> list[ConsolidatedOntologyRow]:
    dataset: list[ConsolidatedOntologyRow] = []

    for metadata in metadata_rows:
        filename = metadata.get("filename", "")
        met = metadata_metrics.get(filename, {})
        papers = sorted(
            papers_by_ontology.get(filename, []),
            key=lambda row: row.get("relevance_score", 0.0),
            reverse=True,
        )
        web_results = sorted(
            web_by_ontology.get(filename, []),
            key=lambda row: row.get("relevance_score", 0.0),
            reverse=True,
        )
        pmet = paper_metrics.get(filename, {})
        wmet = web_metrics.get(filename, {})

        dataset.append(
            ConsolidatedOntologyRow(
                filename=filename,
                title=metadata.get("title", ""),
                prefix=metadata.get("prefix", ""),
                namespace_uri=metadata.get("namespace_uri", ""),
                description=metadata.get("description", ""),
                parse_success=bool(met.get("parse_success", False)),
                metadata_completeness_pct=round(float(met.get("completeness_pct", 0.0)), 1),
                class_count=int(met.get("class_count", 0) or 0),
                property_count=int(met.get("property_count", 0) or 0),
                axiom_count=int(met.get("axiom_count", 0) or 0),
                annotation_coverage_pct=round(float(met.get("annotation_coverage_pct", 0.0)), 1),
                comment_coverage_pct=round(float(met.get("comment_coverage_pct", 0.0)), 1),
                has_license=bool(met.get("has_license", False)),
                has_version=bool(met.get("has_version", False)),
                has_creators=bool(met.get("has_creators", False)),
                has_embedded_doi=bool(DOI_RE.search(metadata.get("see_also", "") or "")),
                paper_count=int(pmet.get("results_returned", len(papers)) or 0),
                top_paper_titles=join_values((row.get("title", "") for row in papers), limit=5),
                top_paper_urls=join_values((row.get("url", "") for row in papers), limit=5),
                paper_match_types=", ".join(f"{k}:{v}" for k, v in count_by(papers, "match_type").items()),
                web_result_count=int(wmet.get("results_returned", len(web_results)) or 0),
                top_web_titles=join_values((row.get("title", "") for row in web_results), limit=5),
                top_web_urls=join_values((row.get("url", "") for row in web_results), limit=5),
                web_result_types=", ".join(f"{k}:{v}" for k, v in count_by(web_results, "result_type").items()),
            )
        )

    return dataset


def build_summary(
    metadata_rows: list[dict],
    metadata_metrics_rows: list[dict],
    paper_rows: list[dict],
    paper_metrics_rows: list[dict],
    web_rows: list[dict],
    web_metrics_rows: list[dict],
) -> dict:
    total = len(metadata_rows)
    parsed = [row for row in metadata_metrics_rows if row.get("parse_success")]
    with_papers = [row for row in paper_metrics_rows if int(row.get("results_returned", 0) or 0) > 0]
    with_web = [row for row in web_metrics_rows if int(row.get("results_returned", 0) or 0) > 0]
    with_doi = [
        row for row in metadata_rows
        if DOI_RE.search(row.get("see_also", "") or "")
    ]
    doi_resolved = [
        row for row in paper_metrics_rows
        if row.get("doi_direct_hit")
    ]

    paper_match_types = Counter(row.get("match_type", "") for row in paper_rows if row.get("match_type"))
    web_result_types = Counter(row.get("result_type", "") for row in web_rows if row.get("result_type"))

    return {
        "total_ontologies": total,
        "parse_success_rate": pct(len(parsed), total),
        "metadata_completeness_avg": round(mean(row.get("completeness_pct", 0.0) for row in metadata_metrics_rows), 1) if metadata_metrics_rows else 0.0,
        "with_title_pct": pct(sum(1 for row in metadata_metrics_rows if row.get("has_title")), total),
        "with_description_pct": pct(sum(1 for row in metadata_metrics_rows if row.get("has_description")), total),
        "with_prefix_pct": pct(sum(1 for row in metadata_rows if row.get("prefix")), total),
        "with_license_pct": pct(sum(1 for row in metadata_metrics_rows if row.get("has_license")), total),
        "with_papers_pct": pct(len(with_papers), total),
        "with_web_results_pct": pct(len(with_web), total),
        "with_doi_pct": pct(len(with_doi), total),
        "with_doi_resolved_pct": pct(len(doi_resolved), total),
        "total_papers": len(paper_rows),
        "total_web_results": len(web_rows),
        "paper_match_type_distribution": dict(sorted(paper_match_types.items())),
        "web_result_type_distribution": dict(sorted(web_result_types.items())),
        "avg_classes_per_ontology": round(mean(row.get("class_count", 0) for row in metadata_metrics_rows), 1) if metadata_metrics_rows else 0.0,
        "avg_properties_per_ontology": round(mean(row.get("property_count", 0) for row in metadata_metrics_rows), 1) if metadata_metrics_rows else 0.0,
        "avg_annotation_coverage": round(mean(row.get("annotation_coverage_pct", 0.0) for row in metadata_metrics_rows), 1) if metadata_metrics_rows else 0.0,
        "avg_comment_coverage": round(mean(row.get("comment_coverage_pct", 0.0) for row in metadata_metrics_rows), 1) if metadata_metrics_rows else 0.0,
        "zero_paper_ontologies": sorted(row.get("ontology_filename", "") for row in paper_metrics_rows if int(row.get("results_returned", 0) or 0) == 0),
        "zero_web_result_ontologies": sorted(row.get("ontology_filename", "") for row in web_metrics_rows if int(row.get("results_returned", 0) or 0) == 0),
    }


def save_summary(summary: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    print(f"  Saved summary -> {path}")


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 60)
    print("Consolidated Dataset Summary")
    print("=" * 60)
    print(f"  Total ontologies          : {summary['total_ontologies']}")
    print(f"  Parse success rate        : {summary['parse_success_rate']}%")
    print(f"  Avg metadata completeness : {summary['metadata_completeness_avg']}%")
    print(f"  With papers               : {summary['with_papers_pct']}%")
    print(f"  With web results          : {summary['with_web_results_pct']}%")
    print(f"  Total papers              : {summary['total_papers']}")
    print(f"  Total web results         : {summary['total_web_results']}")
    print(f"  Avg classes / ontology    : {summary['avg_classes_per_ontology']}")
    print(f"  Avg properties / ontology : {summary['avg_properties_per_ontology']}")
    print("=" * 60 + "\n")


def main() -> None:
    metadata_rows = load_json(DATA_DIR / "ontology_metadata.json", [])
    metadata_metrics_rows = load_json(DATA_DIR / "ontology_metadata_metrics.json", [])
    paper_rows = load_json(DATA_DIR / "ontology_papers.json", [])
    paper_metrics_rows = load_json(DATA_DIR / "ontology_papers_metrics.json", [])
    web_rows = load_json(DATA_DIR / "ontology_web_results.json", [])
    web_metrics_rows = load_json(DATA_DIR / "ontology_web_metrics.json", [])

    metadata_metrics = {row.get("filename", ""): row for row in metadata_metrics_rows}
    paper_metrics = {row.get("ontology_filename", ""): row for row in paper_metrics_rows}
    web_metrics = {row.get("ontology_filename", ""): row for row in web_metrics_rows}
    papers_by_ontology = rows_by_filename(paper_rows, "ontology_filename")
    web_by_ontology = rows_by_filename(web_rows, "ontology_filename")

    dataset = build_dataset(
        metadata_rows,
        metadata_metrics,
        papers_by_ontology,
        paper_metrics,
        web_by_ontology,
        web_metrics,
    )
    summary = build_summary(
        metadata_rows,
        metadata_metrics_rows,
        paper_rows,
        paper_metrics_rows,
        web_rows,
        web_metrics_rows,
    )

    print("[Step 4] Saving consolidated dataset")
    save_csv(dataset, DATA_DIR / "be_ols_dataset.csv")
    save_json(dataset, DATA_DIR / "be_ols_dataset.json")
    save_summary(summary, DATA_DIR / "evaluation_summary.json")
    print_summary(summary)
    print("Done")


if __name__ == "__main__":
    main()
