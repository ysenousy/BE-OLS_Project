# BE-OLS Ontology Research Pipeline

This project builds a research dataset from Built Environment ontologies by:

1. Reading curated ontology metadata from the CyberbuildLab BE-OLS repository (`Ontologies_forRepo.json`).
2. Searching academic papers related to each ontology.
3. Searching related web pages, documentation, repositories, and paper pages.
4. Computing quality and relevance metrics for metadata, paper, and web outputs.

The pipeline is designed for reproducible data collection and evaluation across a large ontology set.

## Project Structure

- `common.py`: Shared dataclasses, similarity function, CSV/JSON save helpers.
- `step1_extract_metadata.py`: Reads the repo's curated `Ontologies_forRepo.json`, maps each record to its TTL filename, outputs metadata + metadata metrics (including the repo's native FOOPS/alignment/quality scores).
- `generate_search_profiles.py`: Regenerates `data/ontology_search_profiles.json` from the step 1 metadata (queries, required/optional terms, exclude terms, known DOIs) so the search keywords stay in sync with the metadata source.
- `step2_search_papers.py`: Builds ontology-specific paper-search queries, queries OpenAlex, resolves DOI links, filters matches, and outputs papers + paper metrics.
- `step3_search_web.py`: Searches DuckDuckGo for ontology-related web pages and outputs web results + web metrics.
- `step4_consolidate.py`: Merges metadata, paper, and web outputs into a consolidated dataset + evaluation summary.
- `plan.md`: Methodology and roadmap for extended phases.
- `Results.txt`: Snapshot of observed run outcomes and summary stats.

Generated outputs:

- `data/ontology_metadata.csv`, `data/ontology_metadata.json`
- `data/ontology_metadata_metrics.csv`, `data/ontology_metadata_metrics.json`
- `data/ontology_search_profiles.json`
- `data/ontology_papers.csv`, `data/ontology_papers.json`
- `data/ontology_papers_metrics.csv`, `data/ontology_papers_metrics.json`
- `data/ontology_web_results.csv`, `data/ontology_web_results.json`
- `data/ontology_web_metrics.csv`, `data/ontology_web_metrics.json`
- `data/be_ols_dataset.csv`, `data/be_ols_dataset.json`
- `data/evaluation_summary.json`

## Data Sources

- Curated ontology metadata: `CyberbuildLab/BE-OLS` file `data/Ontologies_forRepo.json` (auto-generated from the collaborators' `Ontologies.xlsx`).
- TTL filename list (for keying): `CyberbuildLab/BE-OLS` GitHub folder `data/source/Ontologies_TTL` (names only — files are no longer downloaded or parsed).
- Paper APIs:
  - OpenAlex (search)
  - CrossRef (DOI resolution)

## Requirements

Python 3.9+ recommended.

Install dependencies:

```powershell
pip install -r requirements.txt
```

## How to Run

From the `BE-OLS_Project` directory:

### 1) Extract ontology metadata

```powershell
python step1_extract_metadata.py
```

This will:

- Pull the TTL filename list from GitHub (names only, used as the join key).
- Fetch the repo's curated `Ontologies_forRepo.json`.
- Match each `.ttl` filename to its curated record (by prefix/alias/title/URI).
- Map core metadata fields (title, prefix, namespace, description, version, license, creators, imports, etc.) onto the pipeline schema.
- Carry over the repo's native quality metrics (class/property counts, annotation, FOOPS, alignment, accessibility, quality scores).

### 1b) Regenerate search profiles (optional but recommended)

```powershell
python generate_search_profiles.py
```

Rebuilds `data/ontology_search_profiles.json` from `data/ontology_metadata.json`.
Steps 2 and 3 run without this file (they derive queries from metadata directly),
but the profiles restore the `ontology_family` / `reuse_application` paper tiers in
step 2 and add extra query variants. Re-run this whenever step 1 metadata changes.

Note: the generated profiles are derived purely from curated metadata. They do not
reproduce prior hand-tuned strict gates (`required_terms_all`) or manually added
generic acronyms (e.g. bare "BIM"), trading a little curated tuning for full
reproducibility.

### 2) Search related papers

```powershell
python step2_search_papers.py
```

This will:

- Read ontology metadata from `data/ontology_metadata.json`.
- Build ontology-specific search profiles and query variants, scoped by the curated domain signals (`primary_domain`, `secondary_domain`, `cluster`, `conforms_to`).
- Query OpenAlex for each ontology profile.
- Resolve DOI references found in ontology `see_also` links.
- Deduplicate, score, classify, and filter papers.
- Compute per-ontology paper search metrics.

Step 2 is resumable by default. If output files already contain metrics for an ontology,
the script skips that ontology and continues with any missing ones. Useful options:

```powershell
# Rebuild paper outputs from scratch
python step2_search_papers.py --fresh

# Fast run using OpenAlex only
python step2_search_papers.py --fresh --openalex-only

# Process a small batch, then resume later with the same command
python step2_search_papers.py --fresh --max-ontologies 20
python step2_search_papers.py --max-ontologies 20
```

### 3) Search related web results

```powershell
python step3_search_web.py
```

This will:

- Read ontology metadata from `data/ontology_metadata.json`.
- Use ontology-specific search profiles from `data/ontology_search_profiles.json` when available.
- Add domain-scoped query variants from the curated domain signals (`primary_domain`, `secondary_domain`, `cluster`).
- Query DuckDuckGo for web pages, tools, documentation, and project pages.
- Seed official namespace/repository links from ontology metadata.
- Deduplicate, classify, score, and filter web results.
- Compute per-ontology web search metrics.

Step 3 is resumable by default. Useful options:

```powershell
# Rebuild web outputs from scratch
python step3_search_web.py --fresh

# Process a small batch, then resume later
python step3_search_web.py --fresh --max-ontologies 20
python step3_search_web.py --max-ontologies 20
```

### 4) Consolidate outputs

```powershell
python step4_consolidate.py
```

This will:

- Merge ontology metadata, metadata metrics, paper results, and web results.
- Save one row per ontology to `data/be_ols_dataset.csv` and `data/be_ols_dataset.json`.
- Save aggregate coverage metrics to `data/evaluation_summary.json`.

Optional environment settings:

```powershell
$env:PYTHONIOENCODING="utf-8"
$env:HTTP_TIMEOUT="20"
$env:MAX_RETRIES="2"
$env:WEB_SEARCH_DELAY="2.5"
```

## Key Metric Families

### Metadata Metrics

- Parse success/failure
- Completeness percentage
- Class/property/axiom counts
- Annotation and comment coverage
- Import/language counts

### Paper Search Metrics

- Results returned per ontology
- Query-title and query-abstract overlap (Jaccard)
- DOI direct hit flag
- Citation and year statistics
- CS/Engineering ratio
- Open access ratio

### Web Search Metrics

- Results returned per ontology
- Query-title and query-snippet overlap (Jaccard)
- Unique domains
- GitHub result count
- Academic-domain result count
- Result type counts (`official_namespace`, `github_repo`, `paper`, `documentation`, `other`)

## Current Status (from `Results.txt`)

- Parsed 117/119 ontologies successfully (98.3%).
- 2 parsing failures due to malformed/problematic source TTL files.
- 120 cleaned paper results kept across 119 ontologies.
- 44 ontologies had at least 1 paper result.
- 75 ontologies had 0 paper results.
- Current paper matches are classified as `exact_ontology`, `ontology_family`, or `reuse_application`.
- 379 cleaned web results kept across 119 ontologies.
- 115 ontologies had at least 1 web result.
- 4 ontologies had 0 web results: `ph.ttl`, `sao.ttl`, `th-building.ttl`, `wgs84.ttl`.
- Current web results are classified by `result_type`: `official_namespace`, `github_repo`, `paper`, `documentation`, or `other`.
- Consolidated dataset has 119 ontology rows.
- Evaluation summary reports 98.3% parse success, 37.0% with papers, and 96.6% with web results.

## Notes and Limitations

- Pipeline requires internet access (GitHub + external APIs).
- API rate limits are handled with delays/retries, so full runs can take time.
- DuckDuckGo access can be rate-limited; increase `WEB_SEARCH_DELAY` if Step 3 returns repeated search errors.
- Some source TTL files may fail parsing due to upstream syntax/content issues.
- Step 4 consolidation is implemented in `step4_consolidate.py`.

## Reproducibility Tips

- Keep generated CSV/JSON outputs versioned per run date.
- Record API key usage and environment settings when comparing runs.
- Re-run step 1 before step 2 when ontology source data may have changed.
