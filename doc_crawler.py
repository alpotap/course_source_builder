import os
import re
import time
import csv
from urllib.parse import urljoin, urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.microsoft import EdgeChromiumDriverManager

# Configuration
START_URL = "https://docs.microfocus.com/doc/386/25.4/home"
SITE_ORIGIN = "https://docs.microfocus.com"
DOC_PATH_PREFIX = "/doc/"
OUTPUT_DIR = "source"
OUTPUT_FILENAME = "all_doc_links.txt"
TREE_OUTPUT_FILENAME = "doc_links_tree.txt"
EDGES_OUTPUT_FILENAME = "doc_links_edges.csv"
MAX_PAGES = 600
MAX_INTERACTION_ROUNDS = 6
CHECKPOINT_EVERY_NEW_LINK_BATCH = True
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "0"))
SECTION_PRIORITY = [
    "home",
    "releasesummary",
    "useintro",
    "administerolhintro",
    "getstartedintro",
    "integrate",
    "troubleshoot",
    "developintro",
    "install",
    "upgrade",
]


def normalize_doc_url(raw_href):
    if not raw_href:
        return None

    normalized = urljoin(SITE_ORIGIN, raw_href.strip())
    parsed = urlparse(normalized)

    if parsed.netloc != urlparse(SITE_ORIGIN).netloc:
        return None
    if not parsed.path.startswith(DOC_PATH_PREFIX):
        return None

    # Keep path-only identity so anchors/query strings do not duplicate entries.
    clean = parsed._replace(params="", query="", fragment="")
    return urlunparse(clean)


def get_scope_prefix(start_url):
    parsed = urlparse(start_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "doc":
        return f"/doc/{parts[1]}/{parts[2]}/"
    return DOC_PATH_PREFIX


def in_scope(url, scope_prefix):
    parsed = urlparse(url)
    return parsed.path.startswith(scope_prefix)


def setup_driver():
    print("Setting up Edge WebDriver (this may take a moment on first run)...")
    options = webdriver.EdgeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")
    options.add_argument("--window-size=1920,1080")

    setup_errors = []

    # 1) Explicit local driver path (best for locked-down/corporate networks).
    explicit_driver = os.getenv("EDGE_DRIVER_PATH", "").strip()
    if explicit_driver:
        try:
            if os.path.exists(explicit_driver):
                print(f"Using EDGE_DRIVER_PATH: {explicit_driver}")
                return webdriver.Edge(service=EdgeService(explicit_driver), options=options)
            setup_errors.append(f"EDGE_DRIVER_PATH does not exist: {explicit_driver}")
        except Exception as exc:
            setup_errors.append(f"EDGE_DRIVER_PATH failed: {exc}")

    # 2) Local Selenium/driver discovery (PATH or Selenium Manager if available).
    try:
        print("Trying local Edge driver discovery...")
        return webdriver.Edge(options=options)
    except Exception as exc:
        setup_errors.append(f"Local discovery failed: {exc}")

    # 3) Online download via webdriver-manager.
    try:
        print("Trying webdriver-manager download...")
        service = EdgeService(EdgeChromiumDriverManager().install())
        return webdriver.Edge(service=service, options=options)
    except Exception as exc:
        setup_errors.append(f"webdriver-manager download failed: {exc}")

    details = "\n  - " + "\n  - ".join(setup_errors)
    raise RuntimeError(
        "Unable to start Edge WebDriver.\n"
        "Tried: EDGE_DRIVER_PATH, local discovery, webdriver-manager download.\n"
        "If your network blocks driver downloads, install msedgedriver locally and set EDGE_DRIVER_PATH."
        f"{details}"
    )


def wait_until_page_ready(driver, timeout=20):
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def collect_doc_links_on_page(driver):
    links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    discovered = set()
    for link in links:
        try:
            normalized = normalize_doc_url(link.get_attribute("href"))
        except Exception:
            normalized = None
        if normalized:
            discovered.add(normalized)
    return discovered


def collect_doc_links_from_html(driver):
    """Fallback extractor for links embedded in rendered HTML/JS blobs."""
    try:
        html = driver.page_source
    except Exception:
        return set()

    discovered = set()
    # Accept quoted/unquoted tokens that start with /doc/ and stop on common delimiters.
    for token in re.findall(r"/doc/[^\"'\s<>)]*", html):
        normalized = normalize_doc_url(token)
        if normalized:
            discovered.add(normalized)
    return discovered


def wait_for_doc_links(driver, timeout=15):
    end_time = time.time() + timeout
    best_count = 0

    while time.time() < end_time:
        count = len(collect_doc_links_on_page(driver))
        best_count = max(best_count, count)
        if count > 1:
            return count
        time.sleep(0.6)

    return best_count


def write_links_to_file(output_path, links):
    with open(output_path, "w", encoding="utf-8") as f:
        for link in links:
            f.write(f"{link}\n")


def collect_navigation_items(driver):
    """Extract ordered navigation links with explicit parent href when available."""
    script = """
const root = document.querySelector('nav, #nav, .toc, .sidebar, [role="tree"], [role="navigation"]');
if (!root) return [];

const anchors = root.querySelectorAll('a[href]');
const items = [];

function nearestParentHref(anchor, rootNode) {
    let node = anchor.parentElement;
    while (node && node !== rootNode) {
        let direct = null;
        try {
            direct = node.querySelector(':scope > a[href]');
        } catch (_) {
            direct = null;
        }
        if (direct && direct !== anchor) {
            return direct.getAttribute('href') || direct.href;
        }
        node = node.parentElement;
    }
    return null;
}

for (const a of anchors) {
    if (!a) continue;
    const parentHref = nearestParentHref(a, root);

        items.push({
                href: a.getAttribute('href') || a.href,
                text: (a.textContent || '').trim(),
        parentHref: parentHref,
        });
}

return items;
"""
    try:
        return driver.execute_script(script) or []
    except Exception:
        return []


def collect_toc_nodes(driver):
    """Collect links only from the left TOC tree, with explicit visual level."""
    script = """
const root = document.querySelector('#left-panel app-toc, #left-panel .ot-tree, aside#left-panel');
if (!root) return [];

const out = [];
const nodes = root.querySelectorAll('.ot-tree-node');

for (const node of nodes) {
  const anchor = node.querySelector('a[href*="/doc/"]');
  if (!anchor) continue;

  const cls = node.className || '';
    const m = cls.match(/ot-tree-node--level-(\\d+)/);
  const level = m ? parseInt(m[1], 10) : 1;

  out.push({
    href: anchor.getAttribute('href') || anchor.href,
    text: (anchor.textContent || '').trim(),
    level: level,
    nodeId: node.id || '',
  });
}

return out;
"""
    try:
        return driver.execute_script(script) or []
    except Exception:
        return []


def collect_top_level_toc_urls(driver, scope_prefix):
    top = []
    seen = set()
    for rec in collect_toc_nodes(driver):
        if int(rec.get("level", 1) or 1) != 1:
            continue
        url = normalize_doc_url(rec.get("href"))
        if not url or not in_scope(url, scope_prefix) or url in seen:
            continue
        seen.add(url)
        top.append(url)

    priority_index = {slug: idx for idx, slug in enumerate(SECTION_PRIORITY)}

    def sort_key(url):
        slug = url.rsplit("/", 1)[-1]
        return (priority_index.get(slug, 9999), slug)

    return sorted(top, key=sort_key)


def expand_toc_once(driver):
    """Expand collapsed TOC nodes in the left panel."""
    script = """
const root = document.querySelector('#left-panel app-toc, #left-panel .ot-tree, aside#left-panel');
if (!root) return 0;

const selectors = [
  '.ot-tree-node [aria-expanded="false"]',
  '.ot-tree-node .collapsed',
  '.ot-tree-node button[aria-expanded="false"]'
].join(',');

let clicked = 0;
const nodes = root.querySelectorAll(selectors);
for (const node of nodes) {
  if (!node) continue;
  const tag = (node.tagName || '').toLowerCase();
  if (tag === 'a') continue;

  const expandedAttr = node.getAttribute('aria-expanded');
  if (expandedAttr !== null && expandedAttr !== 'false') continue;

  if (node.dataset.copilotExpanded === '1') continue;

  try {
    node.scrollIntoView({block: 'center'});
    node.click();
    node.dataset.copilotExpanded = '1';
    clicked += 1;
  } catch (_) {
    // Continue.
  }
}

return clicked;
"""
    try:
        return int(driver.execute_script(script) or 0)
    except Exception:
        return 0


def expand_section_dfs_step(driver, section_url):
        """Click the next unvisited TOC node in a section subtree (depth-first by DOM order)."""
        section_path = urlparse(section_url).path
        script = """
const targetAbs = arguments[0];
const targetPath = arguments[1];
const root = document.querySelector('#left-panel app-toc, #left-panel .ot-tree, aside#left-panel');
if (!root) return {clicked: false};

function nodeLevel(node) {
    const cls = node.className || '';
    const m = cls.match(/ot-tree-node--level-(\\d+)/);
    return m ? parseInt(m[1], 10) : 1;
}

function nodeHref(node) {
    const a = node.querySelector('a[href*="/doc/"]');
    return a ? (a.href || a.getAttribute('href')) : '';
}

const nodes = Array.from(root.querySelectorAll('.ot-tree-node'));
let sectionIdx = -1;
for (let i = 0; i < nodes.length; i++) {
    const href = nodeHref(nodes[i]);
    if (!href) continue;
    if (href === targetAbs || href.endsWith(targetPath)) {
        sectionIdx = i;
        break;
    }
}
if (sectionIdx < 0) return {clicked: false};

const baseLevel = nodeLevel(nodes[sectionIdx]);

for (let i = sectionIdx; i < nodes.length; i++) {
    const node = nodes[i];
    const level = nodeLevel(node);
    if (i > sectionIdx && level <= baseLevel) break;

    if (node.dataset.copilotDfsDone === '1') continue;

    const btn = node.querySelector('button.ot-tree-node__value');
    if (!btn) {
        node.dataset.copilotDfsDone = '1';
        continue;
    }

    try {
        btn.scrollIntoView({block: 'center'});
        btn.click();
        node.dataset.copilotDfsDone = '1';
        return {clicked: true, href: nodeHref(node)};
    } catch (_) {
        node.dataset.copilotDfsDone = '1';
    }
}

return {clicked: false};
"""
        try:
                result = driver.execute_script(script, section_url, section_path) or {}
                return bool(result.get("clicked", False))
        except Exception:
                return False


def expand_section_fully(driver, section_url, deadline=None, max_steps=300):
        steps = 0
        idle_rounds = 0

        while steps < max_steps:
                if deadline and time.time() >= deadline:
                        break

                clicked = expand_section_dfs_step(driver, section_url)
                if clicked:
                        steps += 1
                        idle_rounds = 0
                        time.sleep(0.5)
                        continue

                idle_rounds += 1
                time.sleep(0.35)
                if idle_rounds >= 2:
                        break

        return steps


def expand_toc_until_stable(driver, deadline=None, max_rounds=120):
    idle_rounds = 0
    for _ in range(max_rounds):
        if deadline and time.time() >= deadline:
            break

        clicked = expand_toc_once(driver)
        if clicked > 0:
            idle_rounds = 0
            time.sleep(0.5)
            continue

        idle_rounds += 1
        time.sleep(0.4)
        if idle_rounds >= 3:
            break


def build_tree_from_toc_levels(toc_records):
    nodes = {}
    children = {}
    parent_of = {}
    roots = []
    stack = []

    for rec in toc_records:
        url = rec["url"]
        title = rec["title"]
        level = max(int(rec.get("level", 1) or 1), 1)

        if len(stack) >= level:
            stack = stack[: level - 1]

        parent = stack[-1] if stack else None

        if url not in nodes:
            nodes[url] = title or url.rsplit("/", 1)[-1]
            if parent:
                parent_of[url] = parent
                children.setdefault(parent, [])
                if url not in children[parent]:
                    children[parent].append(url)
            else:
                roots.append(url)

        if len(stack) == level - 1:
            stack.append(url)
        else:
            stack[level - 1] = url

    return nodes, children, parent_of, roots


def expand_navigation_once(driver):
    """Expand collapsed nav nodes and report the anchor context used to expand them."""
    selectors = [
        '[aria-expanded="false"]',
        '.collapsed',
    ]
    script = """
const selectors = arguments[0];
let clicked = 0;
const expandedParents = [];

function findContextHref(node) {
    const container = node.closest('li, [role="treeitem"], .mat-tree-node, .mat-nested-tree-node, .tree-node');
    if (!container) return null;
    const anchor = container.querySelector('a[href]');
    return anchor ? (anchor.getAttribute('href') || anchor.href) : null;
}

for (const selector of selectors) {
    const nodes = document.querySelectorAll(selector);
    for (const node of nodes) {
        if (!node) continue;
        const tag = (node.tagName || '').toLowerCase();
        if (tag === 'a') continue;
        if (node.dataset.copilotExpanded === '1') continue;

        const expandedAttr = node.getAttribute('aria-expanded');
        if (expandedAttr !== null && expandedAttr !== 'false') continue;

        try {
            const parentHref = findContextHref(node);
            node.scrollIntoView({block: 'center'});
            node.click();
            node.dataset.copilotExpanded = '1';
            clicked += 1;
            if (parentHref) expandedParents.push(parentHref);
        } catch (_) {
            // Keep crawling even if one interaction fails.
        }
    }
}

return { clicked, expandedParents };
"""
    try:
        raw = driver.execute_script(script, selectors) or {}
    except Exception:
        return 0, []

    clicked = int(raw.get("clicked", 0) or 0)
    parents = []
    for href in raw.get("expandedParents", []):
        normalized = normalize_doc_url(href)
        if normalized:
            parents.append(normalized)
    return clicked, parents


def build_tree(nav_records):
    """Build parent-child relationships from ordered (url, title, parent_url) records."""
    nodes = {}
    children = {}
    parent_of = {}
    roots = []

    for rec in nav_records:
        url = rec["url"]
        title = rec["title"]
        parent = rec.get("parent_url")

        if url in nodes:
            # Keep first title; still allow filling missing parent link.
            if url not in parent_of and parent and parent != url:
                parent_of[url] = parent
                children.setdefault(parent, [])
                if url not in children[parent]:
                    children[parent].append(url)
            continue

        nodes[url] = title or url.rsplit("/", 1)[-1]

        if parent:
            parent_of[url] = parent
            children.setdefault(parent, [])
            if url not in children[parent]:
                children[parent].append(url)
        else:
            roots.append(url)

    return nodes, children, parent_of, roots


def write_tree_to_file(tree_path, nodes, children, roots, extras):
    def walk(node, level, lines, seen):
        if node in seen:
            return
        seen.add(node)
        indent = "  " * level
        lines.append(f"{indent}- {nodes.get(node, node)} | {node}")
        for child in children.get(node, []):
            walk(child, level + 1, lines, seen)

    lines = ["Navigation tree (parent -> children):", ""]
    seen = set()
    for root in roots:
        walk(root, 0, lines, seen)

    if extras:
        lines.extend(["", "Unmapped links (not present in nav tree):"])
        for url in extras:
            lines.append(f"- {url}")

    with open(tree_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_edges_csv(csv_path, nodes, children, roots):
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["parent_url", "child_url", "child_title", "child_order"])

        for root_order, root in enumerate(roots, start=1):
            writer.writerow(["", root, nodes.get(root, ""), root_order])

        for parent, child_list in children.items():
            for order, child in enumerate(child_list, start=1):
                writer.writerow([parent, child, nodes.get(child, ""), order])


def ordered_links_from_tree(nodes, children, roots, extras):
    ordered = []
    seen = set()

    def walk(node):
        if node in seen:
            return
        seen.add(node)
        ordered.append(node)
        for child in children.get(node, []):
            walk(child)

    for root in roots:
        walk(root)

    for url in extras:
        if url not in seen:
            ordered.append(url)
            seen.add(url)

    return ordered


def click_nav_link_once(driver, href_value):
    script = """
const hrefValue = arguments[0];
const navSelector = [
  'nav a[href]',
  '#nav a[href]',
  '.toc a[href]',
  '.sidebar a[href]',
  '[role="tree"] a[href]',
  '[role="navigation"] a[href]'
].join(',');

const nodes = document.querySelectorAll(navSelector);
for (const node of nodes) {
  if (!node || node.offsetParent === null) continue;
    if (node.getAttribute('href') === hrefValue || node.href === hrefValue) {
    node.scrollIntoView({block: 'center'});
    node.click();
    return true;
  }
}
return false;
"""
    try:
        return bool(driver.execute_script(script, href_value))
    except Exception:
        return False


def gather_dynamic_links(driver, nav_clicked, scope_prefix, deadline=None):
    discovered_on_page = set()
    nav_records = []
    parent_hints = []
    min_rounds_before_break = 3
    active_parent_context = None

    for round_index in range(MAX_INTERACTION_ROUNDS):
        if deadline and time.time() >= deadline:
            break

        before_count = len(discovered_on_page)
        discovered_on_page.update(collect_doc_links_on_page(driver))
        discovered_on_page.update(collect_doc_links_from_html(driver))
        nav_records.extend(collect_navigation_items(driver))

        expand_count, expanded_parents = expand_navigation_once(driver)
        last_expanded_parent = expanded_parents[-1] if expanded_parents else None
        if last_expanded_parent:
            active_parent_context = last_expanded_parent
        if expand_count:
            time.sleep(0.5)
            after_expand = set(discovered_on_page)
            after_expand.update(collect_doc_links_on_page(driver))
            after_expand.update(collect_doc_links_from_html(driver))
            newly_after_expand = after_expand - discovered_on_page
            discovered_on_page = after_expand
            if last_expanded_parent:
                for child in sorted(newly_after_expand):
                    if child != last_expanded_parent and in_scope(child, scope_prefix):
                        parent_hints.append((last_expanded_parent, child))

        nav_links = driver.find_elements(
            By.CSS_SELECTOR,
            "nav a[href], #nav a[href], .toc a[href], .sidebar a[href], [role='tree'] a[href], [role='navigation'] a[href]",
        )

        clicked_this_round = 0
        for link in nav_links:
            try:
                raw_href = link.get_attribute("href")
                normalized = normalize_doc_url(raw_href)
            except Exception:
                raw_href = None
                normalized = None

            if not raw_href or not normalized or normalized in nav_clicked:
                continue

            if click_nav_link_once(driver, raw_href):
                nav_clicked.add(normalized)
                active_parent_context = normalized
                clicked_this_round += 1
                before_click = set(discovered_on_page)
                time.sleep(0.6)
                discovered_on_page.update(collect_doc_links_on_page(driver))
                discovered_on_page.update(collect_doc_links_from_html(driver))
                nav_records.extend(collect_navigation_items(driver))
                newly_after_click = discovered_on_page - before_click
                for child in sorted(newly_after_click):
                    if child != normalized and in_scope(child, scope_prefix):
                        parent_hints.append((normalized, child))

        # Allow asynchronous navigation trees to finish rendering.
        if clicked_this_round == 0 and expand_count == 0:
            before_idle = set(discovered_on_page)
            time.sleep(0.8)
            discovered_on_page.update(collect_doc_links_on_page(driver))
            discovered_on_page.update(collect_doc_links_from_html(driver))
            nav_records.extend(collect_navigation_items(driver))
            newly_after_idle = discovered_on_page - before_idle
            if active_parent_context:
                for child in sorted(newly_after_idle):
                    if child != active_parent_context and in_scope(child, scope_prefix):
                        parent_hints.append((active_parent_context, child))

        no_new_links = len(discovered_on_page) == before_count
        if (
            round_index + 1 >= min_rounds_before_break
            and no_new_links
            and expand_count == 0
            and clicked_this_round == 0
        ):
            break

    return discovered_on_page, nav_records, parent_hints


def merge_parent_hints(nodes, children, parent_of, parent_hints):
    for parent, child in parent_hints:
        if not parent or not child or parent == child:
            continue

        if child in parent_of:
            continue

        if parent not in nodes:
            nodes[parent] = parent.rsplit("/", 1)[-1] or parent
        if child not in nodes:
            nodes[child] = child.rsplit("/", 1)[-1] or child

        children.setdefault(parent, [])
        if child not in children[parent]:
            children[parent].append(child)
        parent_of[child] = parent


def recompute_roots(nodes, parent_of):
    return [url for url in nodes if url not in parent_of]


def crawl_all_doc_links(start_url):
    start_normalized = normalize_doc_url(start_url)
    if not start_normalized:
        raise ValueError(f"Start URL must be under '{SITE_ORIGIN}{DOC_PATH_PREFIX}'")

    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILENAME)
    tree_output_path = os.path.join(OUTPUT_DIR, TREE_OUTPUT_FILENAME)
    edges_output_path = os.path.join(OUTPUT_DIR, EDGES_OUTPUT_FILENAME)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    scope_prefix = get_scope_prefix(start_url)

    all_links = set()
    toc_records = []
    deadline = time.time() + MAX_RUNTIME_SECONDS if MAX_RUNTIME_SECONDS > 0 else None

    driver = setup_driver()

    try:
        print(f"Scanning root page: {start_normalized}")
        driver.get(start_normalized)
        wait_until_page_ready(driver)
        wait_for_doc_links(driver, timeout=15)

        top_sections = collect_top_level_toc_urls(driver, scope_prefix)
        if not top_sections:
            raise RuntimeError("Could not locate top-level TOC sections in the left navigation panel.")

        # Hierarchy-first traversal: fully expand each top section before moving to the next.
        for idx, section_url in enumerate(top_sections, start=1):
            if deadline and time.time() >= deadline:
                print("Reached runtime limit. Finalizing current results...")
                break

            print(f"Expanding section ({idx}/{len(top_sections)}): {section_url}")
            click_nav_link_once(driver, section_url)
            time.sleep(0.7)
            expanded_steps = expand_section_fully(
                driver,
                section_url,
                deadline=deadline,
                max_steps=220,
            )
            print(f"  - Expanded nodes in section: {expanded_steps}")

            # Capture TOC state after section expansion.
            for rec in collect_toc_nodes(driver):
                normalized = normalize_doc_url(rec.get("href"))
                if not normalized or not in_scope(normalized, scope_prefix):
                    continue
                all_links.add(normalized)

            if CHECKPOINT_EVERY_NEW_LINK_BATCH:
                write_links_to_file(output_path, sorted(all_links))

        # Final pass to ensure collapsed late-loaded nodes are included.
        if not deadline or time.time() < deadline:
            expand_toc_until_stable(driver, deadline=deadline, max_rounds=40)
            for rec in collect_toc_nodes(driver):
                normalized = normalize_doc_url(rec.get("href"))
                if not normalized or not in_scope(normalized, scope_prefix):
                    continue
                all_links.add(normalized)

        # Build tree from a single final TOC snapshot to preserve true hierarchy.
        final_snapshot = collect_toc_nodes(driver)
        for rec in final_snapshot:
            normalized = normalize_doc_url(rec.get("href"))
            if not normalized or not in_scope(normalized, scope_prefix):
                continue
            toc_records.append(
                {
                    "url": normalized,
                    "title": rec.get("text", "").strip(),
                    "level": int(rec.get("level", 1) or 1),
                }
            )
            all_links.add(normalized)

        # De-duplicate while preserving first occurrence and DOM order.
        dedup_records = []
        seen_urls = set()
        for rec in toc_records:
            if rec["url"] in seen_urls:
                continue
            seen_urls.add(rec["url"])
            dedup_records.append(rec)

        nodes, children, parent_of, roots = build_tree_from_toc_levels(dedup_records)

        extras = [url for url in sorted(all_links) if url not in nodes]
        ordered_links = ordered_links_from_tree(nodes, children, roots, extras)

        write_links_to_file(output_path, ordered_links)
        write_tree_to_file(tree_output_path, nodes, children, roots, extras)
        write_edges_csv(edges_output_path, nodes, children, roots)

        print("\nCrawl complete.")
        print(f"Unique scoped links collected: {len(all_links)}")
        print(f"Ordered links saved to: {os.path.abspath(output_path)}")
        print(f"Tree saved to: {os.path.abspath(tree_output_path)}")
        print(f"Edges CSV saved to: {os.path.abspath(edges_output_path)}")

    except KeyboardInterrupt:
        write_links_to_file(output_path, sorted(all_links))
        print("\nCrawl interrupted by user.")
        print(f"Partial links saved: {len(all_links)}")
        print(f"Saved to: {os.path.abspath(output_path)}")

    finally:
        driver.quit()


if __name__ == "__main__":
    print("This script uses Edge + Selenium to extract /doc links, including dynamic menu-generated links.")
    print("Required packages: selenium webdriver-manager")
    print("Install with: pip install selenium webdriver-manager")
    if MAX_RUNTIME_SECONDS > 0:
        print(f"Runtime mode: test (timeout {MAX_RUNTIME_SECONDS} seconds)\n")
    else:
        print("Runtime mode: full scan (no timeout)\n")
    try:
        crawl_all_doc_links(START_URL)
    except Exception as exc:
        print("\nStartup failed:")
        print(exc)
        print(
            "\nTip: If downloads are blocked, install msedgedriver manually and set "
            "EDGE_DRIVER_PATH before running the script."
        )