"""
downloader.py — Downloads doc pages from doc_links_edges.csv into a browsable
local folder hierarchy.

Each child URL is opened in a headless Edge browser, the main content area is
extracted and saved as clean HTML.  Images found in the content are downloaded
locally and src attributes are rewritten to relative paths so the pages work
offline.

Output root (default: docs/)
  index.html              top-level browsable index
  manifest.csv            original CSV + local_path column
  _assets/
    images/               all downloaded images (shared pool)
  <Title>/
    index.html            page content
    <Child Title>/
      index.html
      ...

Configuration (environment variables):
  OUTPUT_DIR          output root folder          (default: docs)
  EDGES_CSV           input CSV path              (default: source/doc_links_edges.csv)
  FORCE_DOWNLOAD      re-download saved pages     (default: 0 — skip existing)
  EDGE_DRIVER_PATH    local msedgedriver.exe path (optional, for offline networks)
  PAGE_LOAD_TIMEOUT   seconds to wait per page    (default: 30)
"""

import argparse
import csv
import hashlib
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EDGES_CSV = os.getenv("EDGES_CSV", os.path.join("source", "doc_links_edges.csv"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "docs")
FORCE_DOWNLOAD = os.getenv("FORCE_DOWNLOAD", "0") == "1"
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "30"))
EDGE_DRIVER_PATH = os.getenv("EDGE_DRIVER_PATH", "")

# Runtime cap — set by -test flag at the command line (0 = no limit)
MAX_RUNTIME_SECONDS: int = 0

# Ordered list of CSS selectors to try when looking for the main content area.
# The first selector that contains meaningful text wins.
CONTENT_SELECTORS = [
    "#right-panel",
    "app-content-view",
    ".ot-content",
    "article",
    "main",
    ".content-area",
    ".content-wrapper",
    "#content",
]

# Editable list of page sections that should be removed before content is saved.
# Use CSS selectors. Tag names like "app-header" and "app-footer" are valid.
EXCLUDED_CONTENT_SELECTORS = [
    "app-header",
    "app-footer",
    "nav",
    "aside",
    "script",
    "style",
    "noscript",
    "div#onetrust-consent-sdk",  # Cookie banner
]

# Minimum text length (chars) required before we consider content loaded.
CONTENT_MIN_CHARS = 200

# ---------------------------------------------------------------------------
# WebDriver setup  (mirrors doc_crawler.py 3-tier fallback)
# ---------------------------------------------------------------------------

def setup_driver() -> webdriver.Edge:
    options = EdgeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    # Suppress console noise
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    setup_errors = []

    # 1) Explicit local driver path (best for locked-down/corporate networks).
    explicit_driver = EDGE_DRIVER_PATH.strip()
    if explicit_driver:
        try:
            if os.path.exists(explicit_driver):
                print(f"Using EDGE_DRIVER_PATH: {explicit_driver}")
                return webdriver.Edge(service=EdgeService(explicit_driver), options=options)
            setup_errors.append(f"EDGE_DRIVER_PATH does not exist: {explicit_driver}")
        except Exception as exc:
            setup_errors.append(f"EDGE_DRIVER_PATH failed: {exc}")

    # 2) Local discovery first: PATH, Selenium Manager, or system-installed driver.
    try:
        print("Trying local Edge driver discovery...")
        return webdriver.Edge(options=options)
    except Exception as exc:
        setup_errors.append(f"Local discovery failed: {exc}")

    # 3) Manual local search in common folders.
    search_dirs = [
        os.path.dirname(os.path.abspath(__file__)),
        os.getcwd(),
        r"C:\Program Files (x86)\Microsoft\Edge\Application",
        r"C:\Program Files\Microsoft\Edge\Application",
    ] + [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]

    for d in search_dirs:
        candidate = os.path.join(d, "msedgedriver.exe")
        if not os.path.isfile(candidate):
            continue
        try:
            print(f"Trying discovered driver: {candidate}")
            return webdriver.Edge(service=EdgeService(candidate), options=options)
        except Exception as exc:
            setup_errors.append(f"Driver at {candidate} failed: {exc}")

    # 4) Online download via webdriver-manager.
    try:
        from webdriver_manager.microsoft import EdgeChromiumDriverManager
        print("Trying webdriver-manager download...")
        return webdriver.Edge(
            service=EdgeService(EdgeChromiumDriverManager().install()),
            options=options,
        )
    except Exception as exc:
        setup_errors.append(f"webdriver-manager download failed: {exc}")

    details = "\n  - " + "\n  - ".join(setup_errors)
    raise RuntimeError(
        "Unable to start Edge WebDriver.\n"
        "Tried: EDGE_DRIVER_PATH, local discovery, common local paths, webdriver-manager download.\n"
        "If your network blocks driver downloads, install msedgedriver locally and set EDGE_DRIVER_PATH."
        f"{details}"
    )

# ---------------------------------------------------------------------------
# Folder-name helpers
# ---------------------------------------------------------------------------

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
_MULTI_SPACE   = re.compile(r'\s+')

def sanitize_folder_name(title: str, max_length: int = 80) -> str:
    """Convert a page title into a safe Windows folder name."""
    name = _INVALID_CHARS.sub("_", title)
    name = _MULTI_SPACE.sub(" ", name).strip(" .")
    return name[:max_length] if name else "_unnamed"

# ---------------------------------------------------------------------------
# CSV loading and path tree building
# ---------------------------------------------------------------------------

def load_edges(csv_path: str) -> list[dict]:
    """Return list of row dicts from the edges CSV."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
    return rows


def build_folder_paths(rows: list[dict]) -> dict[str, str]:
    """
    Return a mapping of child_url → relative folder path under OUTPUT_DIR,
    e.g. 'Use/Customize My Workspace/My Workspace UI'.
    Path is built by walking up the parent chain.
    """
    url_title:  dict[str, str] = {}
    url_parent: dict[str, str | None] = {}

    for row in rows:
        child  = row["child_url"].strip()
        parent = row["parent_url"].strip() or None
        title  = row["child_title"].strip()
        url_title[child]  = title
        url_parent[child] = parent

    def _path_parts(url: str) -> list[str]:
        parts: list[str] = []
        cur: str | None = url
        while cur:
            parts.append(sanitize_folder_name(url_title[cur]))
            cur = url_parent.get(cur)
        parts.reverse()
        return parts

    return {url: os.path.join(*_path_parts(url)) for url in url_title}

# ---------------------------------------------------------------------------
# Page loading helpers
# ---------------------------------------------------------------------------

def _wait_for_angular(driver: webdriver.Edge, timeout: int = PAGE_LOAD_TIMEOUT) -> None:
    """Wait for document.readyState == 'complete' and Angular to settle."""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    # Give Angular a moment to finish rendering after DOM ready
    time.sleep(2)


def find_content_element(driver: webdriver.Edge):
    """
    Try CONTENT_SELECTORS in order and return the first element whose
    text length exceeds CONTENT_MIN_CHARS.  Returns None if none qualify.
    """
    for selector in CONTENT_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in elements:
                if len(el.text) >= CONTENT_MIN_CHARS:
                    return el
        except Exception:
            continue
    return None


def wait_for_content(driver: webdriver.Edge, timeout: int = PAGE_LOAD_TIMEOUT):
    """
    Poll until a content element with meaningful text appears.
    Returns the content element, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        el = find_content_element(driver)
        if el:
            return el
        time.sleep(1)
    # Last chance: try body
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        if len(body.text) >= CONTENT_MIN_CHARS:
            return body
    except Exception:
        pass
    return None


def extract_content_html(driver: webdriver.Edge) -> tuple[str, str]:
    """
    Return (outer_html, selector_used) for the best content element found.
    If no specific container matches, falls back to full body innerHTML.
    """
    def _extract_clean_html(element, return_outer_html: bool) -> str:
        script = """
const node = arguments[0];
const selectors = arguments[1];
const returnOuterHtml = arguments[2];
const clone = node.cloneNode(true);

for (const selector of selectors) {
  try {
    clone.querySelectorAll(selector).forEach((match) => match.remove());
  } catch (error) {
    // Ignore invalid selectors so the rest of the exclusion list still works.
  }
}

return returnOuterHtml ? clone.outerHTML : clone.innerHTML;
"""
        return driver.execute_script(
            script,
            element,
            EXCLUDED_CONTENT_SELECTORS,
            return_outer_html,
        ) or ""

    el = find_content_element(driver)
    if el:
        tag_css = el.tag_name
        try:
            return _extract_clean_html(el, True), tag_css
        except Exception:
            return el.get_attribute("outerHTML") or "", tag_css

    # Fallback: full body
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        return _extract_clean_html(body, False), "body"
    except Exception:
        return "", "none"

# ---------------------------------------------------------------------------
# Image downloading
# ---------------------------------------------------------------------------

def _image_filename(src_url: str) -> str:
    """Derive a safe local filename for an image URL."""
    parsed = urllib.parse.urlparse(src_url)
    basename = os.path.basename(parsed.path) or "image"
    # Strip any query from the basename extension
    basename = basename.split("?")[0]
    # Prefix with a short hash to avoid collisions across pages
    digest = hashlib.md5(src_url.encode()).hexdigest()[:8]
    name, ext = os.path.splitext(basename)
    # Keep the extension (or default to .png)
    ext = ext.lower() if ext else ".png"
    return f"{name}_{digest}{ext}"


def download_image(src_url: str, dest_path: str) -> bool:
    """Download src_url to dest_path.  Returns True on success."""
    if os.path.exists(dest_path):
        return True
    try:
        req = urllib.request.Request(
            src_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; doc-downloader/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return True
    except Exception as exc:
        print(f"    [warn] image download failed: {src_url} — {exc}")
        return False


def rewrite_images(
    content_html: str,
    page_url: str,
    page_folder_abs: str,
    assets_images_abs: str,
) -> str:
    """
    Find all <img src="..."> in content_html, download each image to
    assets_images_abs, and rewrite the src to a relative path back from
    page_folder_abs to assets_images_abs.
    Returns updated HTML string.
    """
    # Relative path from page folder to images assets folder
    rel_assets = os.path.relpath(assets_images_abs, page_folder_abs).replace(os.sep, "/")

    img_pattern = re.compile(r'(<img\b[^>]*?\bsrc=)(["\'])([^"\']*?)(\2)', re.IGNORECASE | re.DOTALL)

    def _img_replacer(m: re.Match) -> str:
        before    = m.group(1)
        quote     = m.group(2)
        orig_src  = m.group(3)
        end_quote = m.group(4)

        if orig_src.startswith("data:"):
            return m.group(0)

        abs_url   = urllib.parse.urljoin(page_url, orig_src)
        filename  = _image_filename(abs_url)
        dest_path = os.path.join(assets_images_abs, filename)

        if download_image(abs_url, dest_path):
            new_src = f"{rel_assets}/{filename}"
            return f"{before}{quote}{new_src}{end_quote}"

        return m.group(0)

    return img_pattern.sub(_img_replacer, content_html)

# ---------------------------------------------------------------------------
# HTML page rendering
# ---------------------------------------------------------------------------

_PAGE_CSS = """
body{font-family:Inter,Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.6;
     color:#222;max-width:980px;margin:0 auto;padding:20px 24px}
nav.breadcrumb{font-size:.85em;color:#555;margin-bottom:12px}
nav.breadcrumb a{color:#2e3d98;text-decoration:none}
nav.breadcrumb a:hover{text-decoration:underline}
nav.breadcrumb span+span::before{content:" › "}
.source-url{font-size:.75em;color:#888;margin-bottom:20px;padding-bottom:12px;
            border-bottom:1px solid #e8e8e8}
.source-url a{color:#888}
.children{margin-top:32px;padding-top:16px;border-top:1px solid #e8e8e8}
.children h2{font-size:1rem;color:#555;margin-bottom:8px}
.children ul{list-style:none;padding:0;margin:0}
.children li{margin:6px 0}
.children li a{color:#2e3d98;text-decoration:none}
.children li a:hover{text-decoration:underline}
img{max-width:100%;height:auto}
pre,code{background:#f4f4f4;border-radius:3px}
pre{padding:12px;overflow-x:auto}
code{padding:1px 4px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:8px 12px;text-align:left}
th{background:#f0f0f0}
"""


def _breadcrumb_html(ancestors: list[tuple[str, str]]) -> str:
    """
    ancestors: list of (title, relative_href) from root to current page.
    """
    if not ancestors:
        return ""
    parts = []
    for title, href in ancestors:
        if href:
            parts.append(f'<span><a href="{href}">{title}</a></span>')
        else:
            parts.append(f'<span>{title}</span>')
    return '<nav class="breadcrumb">' + "".join(parts) + "</nav>"


def _children_html(children: list[tuple[str, str]]) -> str:
    """children: list of (title, relative_href)."""
    if not children:
        return ""
    items = "\n".join(
        f'    <li><a href="{href}">{title}</a></li>'
        for title, href in children
    )
    return f'<div class="children">\n  <h2>In this section</h2>\n  <ul>\n{items}\n  </ul>\n</div>'


def build_html_page(
    title: str,
    source_url: str,
    content_html: str,
    ancestors: list[tuple[str, str]],
    children: list[tuple[str, str]],
) -> str:
    crumb_html    = _breadcrumb_html(ancestors)
    children_block = _children_html(children)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="doc-title" content="{title}">
  <meta name="source-url" content="{source_url}">
  <title>{title}</title>
  <style>{_PAGE_CSS}</style>
</head>
<body>
{crumb_html}
<div class="source-url">Source: <a href="{source_url}">{source_url}</a></div>
<div class="content">
{content_html}
</div>
{children_block}
</body>
</html>
"""


def build_index_page(
    title: str,
    children: list[tuple[str, str]],
    ancestors: list[tuple[str, str]],
) -> str:
    """Index page for a folder that has no directly downloadable content."""
    crumb_html     = _breadcrumb_html(ancestors)
    children_block = _children_html(children)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="doc-title" content="{title}">
  <title>{title}</title>
  <style>{_PAGE_CSS}</style>
</head>
<body>
{crumb_html}
<h1>{title}</h1>
{children_block}
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Root index
# ---------------------------------------------------------------------------

def write_root_index(
    output_root: str,
    root_children: list[tuple[str, str]],  # (title, relative_href)
) -> None:
    items = "\n".join(
        f'  <li><a href="{href}">{title}</a></li>'
        for title, href in root_children
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Documentation Index</title>
  <style>{_PAGE_CSS}</style>
</head>
<body>
<h1>Documentation Index</h1>
<ul>
{items}
</ul>
</body>
</html>
"""
    _write(os.path.join(output_root, "index.html"), html)

# ---------------------------------------------------------------------------
# File writing helper
# ---------------------------------------------------------------------------

def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)

# ---------------------------------------------------------------------------
# Manifest CSV
# ---------------------------------------------------------------------------

def write_manifest_csv(
    rows: list[dict],
    folder_paths: dict[str, str],
    output_root: str,
) -> None:
    out_path = os.path.join(output_root, "manifest.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        fieldnames = ["parent_url", "child_url", "child_title", "child_order", "local_path"]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            child = row["child_url"].strip()
            rel   = folder_paths.get(child, "")
            local = os.path.join(rel, "index.html").replace(os.sep, "/") if rel else ""
            writer.writerow({
                "parent_url":  row["parent_url"],
                "child_url":   child,
                "child_title": row["child_title"],
                "child_order": row["child_order"],
                "local_path":  local,
            })
    print(f"Manifest written → {out_path}")

# ---------------------------------------------------------------------------
# Main download loop
# ---------------------------------------------------------------------------

def download_all(edges_csv_path: str, output_root: str, max_runtime: int = 0) -> None:
    print(f"Loading edges from: {edges_csv_path}")
    rows = load_edges(edges_csv_path)
    print(f"  {len(rows)} pages to process.")

    run_deadline = (time.time() + max_runtime) if max_runtime > 0 else None

    folder_paths = build_folder_paths(rows)

    # Build helper lookups
    url_title:    dict[str, str]       = {r["child_url"].strip(): r["child_title"].strip() for r in rows}
    url_parent:   dict[str, str|None]  = {r["child_url"].strip(): (r["parent_url"].strip() or None) for r in rows}
    url_children: dict[str, list[str]] = {}
    url_order:    dict[str, int]       = {}
    for r in rows:
        parent = r["parent_url"].strip() or None
        child  = r["child_url"].strip()
        order  = int(r.get("child_order", "0") or "0")
        url_order[child] = order
        if parent:
            url_children.setdefault(parent, []).append(child)

    # Sort children of each parent by child_order
    for p in url_children:
        url_children[p].sort(key=lambda u: url_order.get(u, 0))

    # Pre-compute each URL's ancestor chain (root → parent → self)
    def _ancestors(url: str) -> list[str]:
        chain: list[str] = []
        cur: str | None = url
        while cur:
            chain.append(cur)
            cur = url_parent.get(cur)
        chain.reverse()
        return chain

    assets_images_abs = os.path.abspath(os.path.join(output_root, "_assets", "images"))
    os.makedirs(assets_images_abs, exist_ok=True)

    driver = setup_driver()
    total  = len(rows)
    done   = 0
    skipped = 0
    failed  = 0

    try:
        for row in rows:
            if run_deadline and time.time() >= run_deadline:
                print(f"\n[test] Runtime cap reached — stopping after {done} pages.")
                break

            child_url = row["child_url"].strip()
            title     = row["child_title"].strip()
            rel_folder = folder_paths.get(child_url, "")
            if not rel_folder:
                continue

            page_folder_abs = os.path.abspath(os.path.join(output_root, rel_folder))
            page_html_path  = os.path.join(page_folder_abs, "index.html")

            done += 1
            pct = done * 100 // total

            if not FORCE_DOWNLOAD and os.path.exists(page_html_path):
                skipped += 1
                if done % 50 == 0:
                    print(f"  [{pct}%] {done}/{total} — {title} (skip)")
                continue

            print(f"  [{pct}%] {done}/{total} — {title}")

            # ----------------------------------------------------------------
            # Build relative breadcrumb (ancestors)
            # ----------------------------------------------------------------
            ancestor_urls = _ancestors(child_url)  # includes self as last entry
            ancestors_for_crumb: list[tuple[str, str]] = []
            for anc_url in ancestor_urls[:-1]:  # exclude self
                anc_folder = folder_paths.get(anc_url, "")
                if anc_folder:
                    anc_html_abs = os.path.abspath(os.path.join(output_root, anc_folder, "index.html"))
                    rel_href = os.path.relpath(anc_html_abs, page_folder_abs).replace(os.sep, "/")
                else:
                    rel_href = ""
                ancestors_for_crumb.append((url_title.get(anc_url, ""), rel_href))
            # Current page title (no link)
            ancestors_for_crumb.append((title, ""))

            # ----------------------------------------------------------------
            # Build relative children list
            # ----------------------------------------------------------------
            child_urls = url_children.get(child_url, [])
            children_for_nav: list[tuple[str, str]] = []
            for cu in child_urls:
                cu_folder = folder_paths.get(cu, "")
                if cu_folder:
                    cu_html_abs = os.path.abspath(os.path.join(output_root, cu_folder, "index.html"))
                    rel_href = os.path.relpath(cu_html_abs, page_folder_abs).replace(os.sep, "/")
                else:
                    rel_href = ""
                children_for_nav.append((url_title.get(cu, ""), rel_href))

            # ----------------------------------------------------------------
            # Navigate and wait for content
            # ----------------------------------------------------------------
            try:
                driver.get(child_url)
                _wait_for_angular(driver)
                content_el = wait_for_content(driver)
            except Exception as exc:
                print(f"    [error] page load failed: {exc}")
                failed += 1
                # Write a placeholder so we can continue
                placeholder = build_index_page(title, children_for_nav, ancestors_for_crumb)
                os.makedirs(page_folder_abs, exist_ok=True)
                _write(page_html_path, placeholder)
                continue

            # ----------------------------------------------------------------
            # Extract and clean content HTML
            # ----------------------------------------------------------------
            content_html, selector_used = extract_content_html(driver)

            if not content_html.strip():
                print(f"    [warn] empty content — writing index placeholder")
                page_html = build_index_page(title, children_for_nav, ancestors_for_crumb)
            else:
                # Download images and rewrite src attributes
                os.makedirs(assets_images_abs, exist_ok=True)
                content_html = rewrite_images(
                    content_html,
                    child_url,
                    page_folder_abs,
                    assets_images_abs,
                )
                page_html = build_html_page(
                    title,
                    child_url,
                    content_html,
                    ancestors_for_crumb,
                    children_for_nav,
                )

            os.makedirs(page_folder_abs, exist_ok=True)
            _write(page_html_path, page_html)

    except KeyboardInterrupt:
        print("\nInterrupted — writing manifest with progress so far …")
    finally:
        driver.quit()

    # -------------------------------------------------------------------------
    # Write root index
    # -------------------------------------------------------------------------
    root_urls = [r["child_url"].strip() for r in rows if not r["parent_url"].strip()]
    root_children_nav: list[tuple[str, str]] = []
    for rurl in root_urls:
        rf = folder_paths.get(rurl, "")
        if rf:
            rel_href = os.path.join(rf, "index.html").replace(os.sep, "/")
        else:
            rel_href = ""
        root_children_nav.append((url_title.get(rurl, ""), rel_href))

    write_root_index(output_root, root_children_nav)

    # -------------------------------------------------------------------------
    # Write manifest CSV
    # -------------------------------------------------------------------------
    write_manifest_csv(rows, folder_paths, output_root)

    print(
        f"\nDone.  Pages: {done - skipped - failed} downloaded, "
        f"{skipped} skipped, {failed} failed."
    )
    print(f"Output root: {os.path.abspath(output_root)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="downloader.py",
        description="Download doc pages from doc_links_edges.csv into a local folder hierarchy.",
    )
    parser.add_argument(
        "-test",
        metavar="SECONDS",
        type=int,
        default=0,
        help="Run in test mode: stop after SECONDS seconds (0 = full run, default).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=FORCE_DOWNLOAD,
        help="Re-download pages that already exist locally.",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        default=OUTPUT_DIR,
        help=f"Output root directory (default: {OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=EDGES_CSV,
        help=f"Path to edges CSV (default: {EDGES_CSV}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Command-line flags override environment-variable defaults
    edges_csv   = args.csv
    output_dir  = args.output
    force       = args.force
    max_runtime = args.test

    # Propagate force flag to the module-level constant used in the loop
    if force:
        FORCE_DOWNLOAD = True

    mode_label = f"test ({max_runtime}s cap)" if max_runtime else "full run"

    print("=== Doc Downloader ===")
    print(f"  Mode        : {mode_label}")
    print(f"  Input CSV   : {edges_csv}")
    print(f"  Output dir  : {output_dir}")
    print(f"  Force reload: {'yes' if force else 'no (skip existing)'}")
    print(f"  Page timeout: {PAGE_LOAD_TIMEOUT}s\n")

    if not os.path.isfile(edges_csv):
        raise FileNotFoundError(
            f"Edges CSV not found: {edges_csv}\n"
            "Run doc_crawler.py first to generate it."
        )

    download_all(edges_csv, output_dir, max_runtime=max_runtime)
