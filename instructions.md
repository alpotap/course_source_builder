# Project Instructions

## Doc Crawler Tool Definition

### Tool Name
Doc Crawler

### Purpose
The Doc Crawler collects documentation links from a dynamic documentation portal and exports a hierarchical navigation tree that preserves parent-child relationships.

### Primary Goal
Produce machine-readable outputs that allow downstream tools to:
1. Download pages by URL.
2. Store files by section hierarchy.
3. Understand which page belongs to which branch in the documentation tree.

## Scope

### In Scope
1. Open the target documentation site with Selenium.
2. Expand dynamic navigation menus (including deep nested branches).
3. Capture links under the configured documentation scope.
4. Preserve hierarchy using navigation structure, not alphabetical sorting.
5. Export ordered links, tree view, and parent-child edges.
6. Support fast test runs with a runtime limit.

### Out of Scope
1. Downloading page content bodies.
2. Converting pages to markdown/PDF.
3. Mirroring static assets.
4. Any destructive modification of remote content.

## Configuration Contract

The crawler is implemented in [doc_crawler.py](doc_crawler.py) and should use these configuration concepts:

1. `START_URL`: entry page for the documentation set.
2. `SITE_ORIGIN`: origin used for URL normalization.
3. `DOC_PATH_PREFIX` / scope prefix: limits crawl to intended documentation branch.
4. `MAX_RUNTIME_SECONDS`: runtime cap for test iterations.
5. `EDGE_DRIVER_PATH` (optional): local Edge driver path for offline/corporate networks.

## Output Contract

The crawler should write outputs under [source](source):

1. [source/all_doc_links.txt](source/all_doc_links.txt)
Purpose: ordered URLs for sequential processing.

2. [source/doc_links_tree.txt](source/doc_links_tree.txt)
Purpose: human-readable hierarchy (parent -> children).

3. [source/doc_links_edges.csv](source/doc_links_edges.csv)
Purpose: machine-readable parent-child graph for downstream tooling.

## Behavioral Requirements

1. Hierarchy-first traversal:
Expand one top-level section deeply before moving to the next section.

2. Navigation-source truth:
Prefer left navigation tree structure for parent-child relationships.

3. Scope safety:
Do not include links outside the configured documentation scope.

4. Stable identity:
Normalize URLs and remove query/fragment duplication.

5. Checkpoint safety:
Write intermediate output periodically so partial progress is preserved.

6. Timeout finalization:
When runtime limit is reached, finalize outputs cleanly.

## Runtime Modes

1. Test mode:
Use `MAX_RUNTIME_SECONDS=120` (or shorter) to validate behavior quickly.

2. Full mode:
Use `MAX_RUNTIME_SECONDS=0` for uncapped full traversal. This is the default mode and the tool without parameters runs until the scan is complete

## Quality Expectations

1. Deep branches should remain nested under correct top-level sections.
2. No flattening of deep pages into top-level unless truly root nodes.
3. Outputs should be deterministic for the same site state/config.

---

## Doc Downloader Tool Definition

### Tool Name
Doc Downloader

### Purpose
The Doc Downloader renders each documentation page listed in `doc_links_edges.csv` using a headless browser, extracts the main content area, and saves it as clean HTML in a local folder hierarchy that mirrors the documentation's parent-child structure.

### Primary Goal
Produce a local, human-browsable copy of the documentation that:
1. Preserves headings, body text, tables, code blocks, and images.
2. Organises files into folders named after their titles in the CSV.
3. Is parseable by downstream automation tools via a manifest CSV that maps every source URL to its local file path.

## Scope

### In Scope
1. Open each `child_url` from `doc_links_edges.csv` in a headless browser.
2. Wait for Angular/SPA content to fully render.
3. Extract only the main content area (not the navigation menus).
4. Download images referenced in the content and rewrite src attributes to local relative paths.
5. Save each page as `index.html` inside a folder named after its `child_title`.
6. Nest folders to match the parent-child hierarchy in the CSV.
7. Generate a browsable `index.html` at the output root.
8. Write `manifest.csv` (edges CSV + `local_path` column) for downstream tooling.
9. Support resume: skip pages whose `index.html` already exists unless `FORCE_DOWNLOAD=1`.

### Out of Scope
1. Expanding or interacting with the site's navigation menus.
2. Converting content to Markdown or PDF.
3. Crawling links not listed in the edges CSV.
4. Any write operations to the remote site.

## Configuration Contract

The downloader is implemented in [downloader.py](downloader.py) and uses these environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `EDGES_CSV` | `source/doc_links_edges.csv` | Input edges CSV produced by Doc Crawler |
| `OUTPUT_DIR` | `docs` | Root folder for downloaded pages |
| `FORCE_DOWNLOAD` | `0` | Set to `1` to re-download pages that already exist |
| `PAGE_LOAD_TIMEOUT` | `30` | Seconds to wait for each page to render |
| `EDGE_DRIVER_PATH` | *(empty)* | Full path to `msedgedriver.exe` for offline/corporate networks |

The downloader also exposes one editable in-code cleanup list:

| Setting | Location | Purpose |
|---|---|---|
| `EXCLUDED_CONTENT_SELECTORS` | [downloader.py](downloader.py) | CSS selectors that are removed from the extracted HTML before saving |

## Output Contract

All outputs are written under the configured `OUTPUT_DIR` (default: [docs](docs)):

1. `docs/index.html`
   Purpose: top-level browsable index linking to all root sections.

2. `docs/manifest.csv`
   Purpose: machine-readable map of every page — columns: `parent_url`, `child_url`, `child_title`, `child_order`, `local_path`.
   `local_path` is relative to `OUTPUT_DIR` and always ends in `index.html`.

3. `docs/<Title>/index.html` (nested per hierarchy)
   Purpose: rendered page content in clean HTML.
   Each file includes:
   - Breadcrumb navigation back to parent and root.
   - `<meta name="source-url">` and `<meta name="doc-title">` for tool parsing.
   - "In this section" child links at the bottom of the page.
   - Images rewritten to relative paths under `docs/_assets/images/`.

4. `docs/_assets/images/`
   Purpose: shared pool of all downloaded images.

## Behavioral Requirements

1. Content-first extraction:
   Try CSS selectors in this order to find the main content: `#right-panel`, `app-content-view`, `.ot-content`, `article`, `main`, `.content-area`, `body`.
   Pick the first element whose text length exceeds 200 characters.

2. SPA render wait:
   After `document.readyState == 'complete'`, wait an additional 2 seconds for Angular to finish, then poll for the content element up to `PAGE_LOAD_TIMEOUT` seconds.

3. Folder naming:
   Sanitize `child_title` by replacing Windows-forbidden characters (`\ / : * ? " < > |`) with underscores and truncating to 80 characters.

4. Image handling:
   Download images to `_assets/images/<basename>_<md5hash8>.ext`.  Rewrite `<img src>` to a relative path from the page folder to `_assets/images/`.  On download failure, keep the original src value unchanged.

5. Resume safety:
   Skip any page whose `index.html` already exists unless `FORCE_DOWNLOAD=1`.  Write the root `index.html` and `manifest.csv` at the end of every run, reflecting all completed pages.

6. Failure isolation:
   If a page fails to load, write a placeholder `index.html` with the title and child links, log the error, and continue with the next page.

7. Exclusion list support:
   Remove all elements matching `EXCLUDED_CONTENT_SELECTORS` from the extracted HTML before saving. This list is meant to be edited as site-specific cleanup rules evolve.

## Runtime Modes

Run normally (resumes from where a previous run left off):
```
py downloader.py
```

Force re-download of all pages:
```
set FORCE_DOWNLOAD=1 && py downloader.py
```

Custom output folder:
```
set OUTPUT_DIR=my_docs && py downloader.py
```

## Quality Expectations

1. Folder hierarchy must exactly mirror the parent-child relationships in the CSV.
2. Every page in the CSV must have a corresponding `index.html` or a logged failure.
3. `manifest.csv` must contain a valid `local_path` for every row where download succeeded.
4. Images must render correctly when the HTML is opened in a local browser (relative paths must resolve).
5. All six HTML `<meta>` tags (`charset`, `viewport`, `doc-title`, `source-url`) must be present on every page.

---

## Working Agreement For This Repository

When making changes in this project, always read and follow this file first.

Required workflow for future work:
1. Check [instructions.md](instructions.md) before implementing changes.
2. Ensure new code and outputs remain consistent with the scope and contracts above.
3. If requirements evolve, update this file in the same change set.

