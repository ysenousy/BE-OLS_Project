# Plan: BE-OLS Ontology Research Pipeline

## TL;DR
Replace the current 2-step pipeline (LOV catalog + GitHub search) with a new 3-step pipeline that:
1. Fetches TTL files from the CyberbuildLab/BE-OLS GitHub repo and extracts metadata using `rdflib`
2. Searches Semantic Scholar for related research papers using extracted keywords
3. Searches DuckDuckGo for related web results (projects, tools, documentation)
4. Saves a unified dataset per ontology (metadata + papers + web results)

## Source
~120 TTL files from: https://github.com/CyberbuildLab/BE-OLS/tree/main/data/source/Ontologies_TTL

## Key Decisions
- TTL files fetched on-the-fly via GitHub raw URLs (no local clone)
- Search both academic papers (Semantic Scholar) AND web results (DuckDuckGo)
- Extract title + IRI + description keywords from TTL metadata for search queries
- Collect top 10 results per ontology per source
- Replace existing step1/step2 entirely

---

## Steps

### Phase 1: Common Data Model & Utilities (`common.py` rewrite)

1. **Rewrite `OntologyRow` dataclass** to reflect new metadata fields extracted from TTL:
   - `filename` (str): TTL filename (e.g., "bot.ttl")
   - `title` (str): from `dcterms:title`
   - `prefix` (str): from `vann:preferredNamespacePrefix`
   - `namespace_uri` (str): from `vann:preferredNamespaceUri`
   - `description` (str): from `dcterms:description`
   - `version` (str): from `owl:versionInfo`
   - `license` (str): from `dcterms:license`
   - `creators` (str): from `dcterms:creator` (comma-separated)
   - `date_modified` (str): from `dcterms:modified`
   - `date_issued` (str): from `dcterms:issued`
   - `see_also` (str): from `rdfs:seeAlso` (comma-separated URIs, includes paper DOIs)
   - `imports` (str): from `owl:imports`
   - `classes` (str): top owl:Class local names
   - `properties` (str): top owl:ObjectProperty / owl:DatatypeProperty local names
   - `search_keywords` (str): derived search keywords for Steps 2 & 3

2. **Add `PaperResult` dataclass** for Semantic Scholar results:
   - `ontology_filename`, `search_query`, `paper_id`, `title`, `authors`, `year`, `venue`, `citation_count`, `abstract`, `doi`, `url`, `is_open_access`, `fields_of_study`

3. **Add `WebResult` dataclass** for DuckDuckGo results:
   - `ontology_filename`, `search_query`, `title`, `url`, `snippet`

### Phase 1b: Evaluation Metric Dataclasses (`common.py`)

1b. **Add `MetadataMetrics` dataclass** — computed per ontology in step1:
   - `filename` (str)
   - `fields_total` (int): total metadata fields checked (15)
   - `fields_filled` (int): how many have non-empty values
   - `completeness_pct` (float): `fields_filled / fields_total * 100`
   - `parse_success` (bool): whether rdflib parsed without error
   - `parse_error` (str): error message if parsing failed
   - `class_count` (int): number of owl:Class entities
   - `property_count` (int): number of owl:ObjectProperty + owl:DatatypeProperty
   - `axiom_count` (int): total triples in the graph
   - `annotation_coverage_pct` (float): % of classes+properties that have rdfs:label
   - `comment_coverage_pct` (float): % of classes+properties that have rdfs:comment
   - `import_count` (int): number of owl:imports
   - `language_count` (int): distinct language tags on rdfs:label/rdfs:comment
   - `has_license` (bool), `has_version` (bool), `has_creators` (bool)

1c. **Add `PaperSearchMetrics` dataclass** — computed per ontology in step2:
   - `ontology_filename`, `search_query`
   - `results_returned` (int): how many papers returned (0–10)
   - `avg_title_keyword_overlap` (float): mean Jaccard similarity between search keywords and each paper title
   - `avg_abstract_keyword_overlap` (float): mean Jaccard similarity with abstracts
   - `max_title_overlap` (float): best single-paper title match
   - `doi_direct_hit` (bool): whether a DOI from rdfs:seeAlso was found on Semantic Scholar
   - `avg_citation_count` (float): mean citations across results
   - `median_year` (int): median publication year
   - `cs_engineering_ratio` (float): fraction of papers with Computer Science or Engineering in fieldsOfStudy
   - `open_access_ratio` (float): fraction with open-access PDF

1d. **Add `WebSearchMetrics` dataclass** — computed per ontology in step3:
   - `ontology_filename`, `search_query`
   - `results_returned` (int)
   - `avg_title_keyword_overlap` (float): Jaccard similarity between query terms and result titles
   - `avg_snippet_keyword_overlap` (float): Jaccard similarity with snippets
   - `unique_domains` (int): number of distinct URL domains in results
   - `github_result_count` (int): how many results point to github.com
   - `academic_result_count` (int): how many results point to academic domains (scholar, doi.org, researchgate, etc.)

### Phase 2: TTL Metadata Extraction (`step1_extract_metadata.py`)

4. **Build TTL file list** — Fetch the directory listing from the GitHub API (`GET /repos/CyberbuildLab/BE-OLS/contents/data/source/Ontologies_TTL`) to get all `.ttl` filenames and their raw download URLs.

5. **Download & parse each TTL file** using `rdflib`:
   - `graph.parse(url, format="turtle")`
   - Extract ontology-level triples (the subject that is `a owl:Ontology`)
   - Extract: `dcterms:title`, `dcterms:description`, `vann:preferredNamespacePrefix`, `vann:preferredNamespaceUri`, `dcterms:creator`, `dcterms:license`, `owl:versionInfo`, `dcterms:modified`, `dcterms:issued`, `rdfs:seeAlso`, `owl:imports`
   - Extract class names: subjects where `?s a owl:Class` and `?s rdfs:isDefinedBy <ontologyIRI>`
   - Extract property names: subjects where `?s a owl:ObjectProperty` or `owl:DatatypeProperty`
   - Handle missing metadata gracefully (many TTL files may lack some fields)

6. **Derive search keywords** per ontology:
   - Primary: ontology title (if available)
   - Secondary: prefix + "ontology" (e.g., "BOT ontology")
   - Tertiary: first sentence of description (truncated to ~100 chars)
   - If title contains domain terms, use those as-is

7. **Output** — Save to `ontology_metadata.csv` and `ontology_metadata.json` with all extracted fields

7b. **Compute & output metadata evaluation metrics** per ontology:
    - For each `OntologyRow`, count filled vs empty fields → `completeness_pct`
    - Count `owl:Class`, `owl:ObjectProperty`, `owl:DatatypeProperty` → `class_count`, `property_count`
    - Count total triples → `axiom_count`
    - Check which classes/properties have `rdfs:label` → `annotation_coverage_pct`
    - Check which classes/properties have `rdfs:comment` → `comment_coverage_pct`
    - Count distinct language tags on labels/comments → `language_count`
    - Count `owl:imports` → `import_count`
    - Booleans: `has_license`, `has_version`, `has_creators`
    - Log parse errors → `parse_success`, `parse_error`
    - Save to `ontology_metadata_metrics.csv` / `.json`
    - Print summary to console:
      - Total ontologies parsed / failed
      - Mean completeness % across all ontologies
      - Distribution: how many have title, description, prefix, license, version
      - Mean class count, property count, axiom count
      - Mean annotation coverage, comment coverage

### Phase 3: Academic Paper Search (`step2_search_papers.py`)

8. **For each ontology**, query Semantic Scholar relevance search API:
   - Endpoint: `GET https://api.semanticscholar.org/graph/v1/paper/search`
   - Query: ontology title or prefix-based keywords
   - Fields: `title,authors,year,venue,citationCount,abstract,externalIds,url,isOpenAccess,fieldsOfStudy`
   - Limit: 10 results per query
   - Filter: `fieldsOfStudy=Computer Science,Engineering` (optional, to focus results)

9. **Also check embedded DOIs** — If `rdfs:seeAlso` contains DOI-like URIs (e.g., `https://doi.org/...`), look up those papers directly via `GET /paper/DOI:{doi}` for authoritative results.

10. **Rate limiting** — Semantic Scholar free tier allows ~100 requests/5 minutes (1 req/3sec). Implement sleep + retry with exponential backoff. Optionally support `x-api-key` header for higher limits.

11. **Output** — Save to `ontology_papers.csv` and `ontology_papers.json`

11b. **Compute & output paper search evaluation metrics** per ontology:
    - `results_returned`: count of papers returned (flag ontologies with 0 results)
    - `avg_title_keyword_overlap`: tokenize search query and each paper title → compute mean Jaccard index
    - `avg_abstract_keyword_overlap`: same with abstracts (skip if abstract is None)
    - `max_title_overlap`: best single-paper title match score
    - `doi_direct_hit`: True if any DOI from `rdfs:seeAlso` was successfully resolved
    - `avg_citation_count`: mean citations across returned papers
    - `median_year`: median publication year of results
    - `cs_engineering_ratio`: fraction of results where `fieldsOfStudy` contains "Computer Science" or "Engineering"
    - `open_access_ratio`: fraction of results with `isOpenAccess=True`
    - Save to `ontology_papers_metrics.csv` / `.json`
    - Print summary to console:
      - Total ontologies with ≥1 paper found / 0 papers found
      - Mean results per ontology
      - Mean keyword overlap (title), mean keyword overlap (abstract)
      - Mean citation count, median year across all results
      - % ontologies with direct DOI hit

### Phase 4: Web Search (`step3_search_web.py`)

12. **For each ontology**, query DuckDuckGo via the `duckduckgo-search` (or `ddgs`) Python package:
    - `DDGS().text(keywords=query, max_results=10)`
    - Query: ontology title + "ontology" or prefix + "built environment ontology"
    - Returns: list of `{title, href, body}` dicts

13. **Rate limiting** — Add 2-3 second delay between searches to avoid being rate-limited by DuckDuckGo.

14. **Output** — Save to `ontology_web_results.csv` and `ontology_web_results.json`

14b. **Compute & output web search evaluation metrics** per ontology:
    - `results_returned`: count of results (flag ontologies with 0)
    - `avg_title_keyword_overlap`: Jaccard index between query tokens and result title tokens
    - `avg_snippet_keyword_overlap`: same with snippet/body text
    - `unique_domains`: number of distinct base domains (e.g., github.com, w3.org) in result URLs
    - `github_result_count`: results pointing to github.com
    - `academic_result_count`: results pointing to known academic domains (doi.org, researchgate.net, semanticscholar.org, springer.com, ieeexplore.ieee.org, arxiv.org, etc.)
    - Save to `ontology_web_metrics.csv` / `.json`
    - Print summary to console:
      - Total ontologies with ≥1 web result / 0 results
      - Mean results per ontology
      - Mean keyword overlap (title), mean keyword overlap (snippet)
      - Mean unique domains per ontology
      - Distribution of GitHub vs academic vs other results

### Phase 5: Dataset Consolidation (optional `step4_consolidate.py`)

15. **Merge all outputs** into a single unified dataset:
    - Per ontology: metadata + paper count + top paper titles + web result count + top web result URLs
    - Save as `be_ols_dataset.csv` and `be_ols_dataset.json`

15b. **Compute aggregated dataset coverage metrics** (printed + saved):
    - `total_ontologies`: total TTL files processed
    - `parse_success_rate`: % parsed without errors
    - `metadata_completeness_avg`: mean completeness across all ontologies
    - `with_title_pct`: % of ontologies that have a title
    - `with_description_pct`: % with a description
    - `with_prefix_pct`: % with a namespace prefix
    - `with_license_pct`: % with a license
    - `with_papers_pct`: % with ≥1 paper result
    - `with_web_results_pct`: % with ≥1 web result
    - `with_doi_pct`: % that have embedded DOIs in rdfs:seeAlso
    - `with_doi_resolved_pct`: % where the DOI was found on Semantic Scholar
    - `total_papers`: total papers collected across all ontologies
    - `total_web_results`: total web results collected
    - `avg_classes_per_ontology`: mean class count
    - `avg_properties_per_ontology`: mean property count
    - `avg_annotation_coverage`: mean % of classes/properties with rdfs:label
    - Save summary to `evaluation_summary.json`
    - Print a formatted report table to console

---

## Relevant Files

### To modify / replace:
- `common.py` — Rewrite dataclasses (OntologyRow → new fields, add PaperResult, WebResult)
- `step1_builtenv_catalog.py` — Replace entirely with `step1_extract_metadata.py`
- `step2_github_ontology.py` — Replace entirely with `step2_search_papers.py`

### New files to create:
- `step1_extract_metadata.py` — TTL parsing and metadata extraction
- `step2_search_papers.py` — Semantic Scholar academic paper search
- `step3_search_web.py` — DuckDuckGo web search
- `step4_consolidate.py` — (Optional) Merge all outputs

### Output files (generated):
- `ontology_metadata.csv` / `.json` — Extracted TTL metadata for all ~120 ontologies
- `ontology_metadata_metrics.csv` / `.json` — Per-ontology metadata quality & ontology complexity metrics
- `ontology_papers.csv` / `.json` — Academic papers per ontology
- `ontology_papers_metrics.csv` / `.json` — Per-ontology paper search relevance metrics
- `ontology_web_results.csv` / `.json` — Web search results per ontology
- `ontology_web_metrics.csv` / `.json` — Per-ontology web search relevance metrics
- `be_ols_dataset.csv` / `.json` — (Optional) Consolidated dataset
- `evaluation_summary.json` — Aggregated coverage metrics across all ontologies

---

## Dependencies (Python packages)

| Package | Purpose | Install |
|---------|---------|---------|
| `rdflib` | Parse TTL/RDF files | `pip install rdflib` |
| `requests` | HTTP calls to GitHub API, Semantic Scholar | `pip install requests` |
| `duckduckgo-search` | DuckDuckGo web search | `pip install duckduckgo-search` |

---

## Verification

1. **Step 1 check**: Run `step1_extract_metadata.py` → verify `ontology_metadata.csv` contains ~120 rows with titles, prefixes, and descriptions for known ontologies (e.g., BOT should have title "The Building Topology Ontology (BOT)", prefix "bot")
2. **Step 1 metrics check**: Verify `ontology_metadata_metrics.csv` is generated; mean completeness should be > 40%; BOT should have class_count=5/≥5, annotation_coverage ≈ 100%, language_count ≥ 10
3. **Step 2 check**: Run `step2_search_papers.py` → verify `ontology_papers.csv` has ~10 paper results per ontology; spot-check that BOT returns papers about "Building Topology Ontology"
4. **Step 2 metrics check**: Verify `ontology_papers_metrics.csv`; BOT should have `doi_direct_hit=True`, `max_title_overlap > 0.5`; mean `cs_engineering_ratio > 0.3` across all ontologies
5. **Step 3 check**: Run `step3_search_web.py` → verify `ontology_web_results.csv` has ~10 web results per ontology; spot-check that results are relevant
6. **Step 3 metrics check**: Verify `ontology_web_metrics.csv`; mean `avg_title_keyword_overlap > 0.1`; `unique_domains > 3` on average
7. **Coverage check**: Run `step4_consolidate.py` → verify `evaluation_summary.json` shows `parse_success_rate > 95%`, `with_papers_pct > 70%`, `with_web_results_pct > 80%`
8. **Edge cases**: Check ontologies with minimal metadata (e.g., no `dcterms:title`) still produce usable search queries via prefix fallback, and metrics correctly flag low completeness

---

## API Details

### Semantic Scholar (Free, no key required for basic use)
- **Relevance search**: `GET https://api.semanticscholar.org/graph/v1/paper/search?query={query}&fields={fields}&limit=10`
- **Paper by DOI**: `GET https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields={fields}`
- **Rate limit** (unauthenticated): ~100 requests / 5 minutes
- **Rate limit** (with API key via `x-api-key` header): ~1 request/second sustained

### DuckDuckGo (via `duckduckgo-search` package)
- `DDGS().text(keywords, max_results=10)` → `[{title, href, body}, ...]`
- No API key needed; rate limiting handled by adding delays between requests
- Note: package renamed to `ddgs` (`pip install ddgs`) but `duckduckgo-search` still works

### GitHub Contents API (for file listing)
- `GET https://api.github.com/repos/CyberbuildLab/BE-OLS/contents/data/source/Ontologies_TTL`
- Returns JSON array with `name`, `download_url` for each file
- Raw file URL pattern: `https://raw.githubusercontent.com/CyberbuildLab/BE-OLS/main/data/source/Ontologies_TTL/{filename}`

---

## TTL Metadata Fields to Extract (RDF predicates)
- `dcterms:title` → title
- `dcterms:description` → description  
- `vann:preferredNamespacePrefix` → prefix
- `vann:preferredNamespaceUri` → namespace URI
- `dcterms:creator` → creators (may be literals or URIs with `vcard:fn`)
- `dcterms:license` → license
- `owl:versionInfo` → version
- `dcterms:modified` → date modified
- `dcterms:issued` → date issued
- `rdfs:seeAlso` → references (paper URLs, DOIs)
- `owl:imports` → imported ontologies
- Subjects with `rdf:type owl:Class` → class names
- Subjects with `rdf:type owl:ObjectProperty` or `owl:DatatypeProperty` → property names
