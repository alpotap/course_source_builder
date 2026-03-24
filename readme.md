# Doc Scraper

A two-step toolchain that maps and downloads an Angular documentation site into a local, browsable HTML copy.

---

## Quick start

```
py tool.py
```

This opens an interactive wizard.  It asks which tool to run, prompts for any settings that differ from the last run, and then launches the tool.  Settings are remembered automatically.

---

## Tools

### 1 · URL Scanner — `doc_crawler.py`

Opens the target docs site in a headless browser, expands the entire navigation tree section by section, and records every page link it finds.

**Outputs** (written to `source/`):

| File | Purpose |
|---|---|
| `all_doc_links.txt` | Ordered list of URLs, one per line |
| `doc_links_tree.txt` | Indented hierarchy for human reading |
| `doc_links_edges.csv` | Parent → child table used by the downloader |

**Run directly:**
```
py doc_crawler.py                          # full scan (default)
set MAX_RUNTIME_SECONDS=120 && py doc_crawler.py   # test run, stops after 2 min
```

---

### 2 · Page Downloader — `downloader.py`

Reads `doc_links_edges.csv` and downloads every listed page into a local folder tree, named and nested by section title.

**Outputs** (written to `docs/` by default):

| Path | Purpose |
|---|---|
| `docs/index.html` | Browsable root index |
| `docs/manifest.csv` | Full page map with local file paths — for tools |
| `docs/<Section>/…/index.html` | Each downloaded page |
| `docs/_assets/images/` | All images used across all pages |

Each saved page contains breadcrumb links, a link back to the live source URL, and a "In this section" list of child pages.

To exclude unwanted sections from saved pages, edit `EXCLUDED_CONTENT_SELECTORS` in [downloader.py](downloader.py). Typical examples: `app-header`, `app-footer`, `nav`, `aside`.

**Run directly:**
```
py downloader.py              # full download, skip pages already saved
py downloader.py -test 45     # test run, stops after 45 seconds
py downloader.py --force      # re-download everything
py downloader.py --output my_docs   # write to a different folder
```

Interrupted downloads resume automatically — only missing pages are fetched on the next run.

---

### 3 · Wizard — `tool.py`

Interactive menu that wraps both tools.  Remembers your last settings so you only need to answer questions when something changes.

```
py tool.py
```

Menu options:
1. Run URL Scanner
2. Run Page Downloader
3. Run both in sequence

Settings are saved to `.toolstate` in the project folder.

---

## Configuration

All tools support these environment variables as overrides:

| Variable | Default | Used by |
|---|---|---|
| `START_URL` | `https://docs.microfocus.com/doc/386/25.4/home` | Crawler |
| `MAX_RUNTIME_SECONDS` | `0` (no limit) | Crawler |
| `EDGES_CSV` | `source/doc_links_edges.csv` | Downloader |
| `OUTPUT_DIR` | `docs` | Downloader |
| `FORCE_DOWNLOAD` | `0` | Downloader |
| `PAGE_LOAD_TIMEOUT` | `30` | Downloader |
| `EDGE_DRIVER_PATH` | *(auto-detect)* | Both |

The downloader also has one code-level setting for cleanup:

| Setting | Location | Purpose |
|---|---|---|
| `EXCLUDED_CONTENT_SELECTORS` | [downloader.py](downloader.py) | CSS selectors removed from downloaded page content before saving |

### Offline / corporate networks

If the machine cannot reach the internet to download a driver, set `EDGE_DRIVER_PATH` to the full path of `msedgedriver.exe` before running:

```
set EDGE_DRIVER_PATH=C:\tools\msedgedriver.exe
py tool.py
```

---

## Typical first run

```
# Step 1 – map the site (full scan, ~20–40 min)
py doc_crawler.py

# Step 2 – download all pages (~1–3 hours depending on site speed)
py downloader.py

# Then open docs/index.html in a browser
```

Or use the wizard for both steps:

```
py tool.py   →   choose option 3 (Run both)
```
