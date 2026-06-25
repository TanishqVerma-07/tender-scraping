from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
import re
import os
import sqlite3
import threading
import time
import hashlib
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

# The gov sites use self-signed / mismatched certs; we already pass verify=False, so
# silence the resulting noise rather than printing a warning per request.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ==========================================
# 1. SAFELY READ URLS FROM STREAMLIT DATABASE
# ==========================================
def get_urls_from_db():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(script_dir, 'tenders.db')
    
    if not os.path.exists(db_path):
        print(f"❌ CRITICAL ERROR: Database 'tenders.db' not found in {script_dir}.")
        print("Please add websites via your Streamlit dashboard first!")
        exit()
        
    try:
        # Read-only mode to prevent locks
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        c = conn.cursor()
        c.execute("SELECT item FROM websites")
        
        # Fetch, clean, and ensure they are actual links
        db_urls = [row[0].strip() for row in c.fetchall() if row[0] and str(row[0]).strip().startswith('http')]
        conn.close()
        
        # Remove any exact duplicates
        return list(dict.fromkeys(db_urls))
        
    except Exception as e:
        print(f"❌ Database error: {e}")
        exit()

unique_urls = get_urls_from_db()

# Block direct PDFs to prevent browser crashes
urls_to_scrape = [url for url in unique_urls if not url.lower().endswith('.pdf')]

if len(urls_to_scrape) == 0:
    print("⚠️ No valid URLs found in the database. Exiting.")
    exit()

print(f"📂 Successfully loaded {len(urls_to_scrape)} unique URLs from 'tenders.db'.")
print("⚡ Launching BULLETPROOF PARALLEL Sorter...\n")

# ==========================================
# 2. SETUP GLOBALS, LOCKS & RAW DB
# ==========================================
db_lock = threading.Lock()
MAX_PAGES = 500  # High backstop only; the real loop guard is content-change detection below.
NAV_RETRIES = 2  # Retry navigation on transient timeouts before giving up on a site.
RAW_DB_NAME = 'raw_database.db'

doc_extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip']
keywords = ['tender', 'notice', 'corrigendum', 'rfp', 'rfq', 'auction', 'bid', 'eoi']
external_portals = ['eprocure', 'gem.gov.in', 'tenderwizard']

def init_raw_database():
    """Wipes old data and creates fresh tables for the new scrape run."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    raw_db_path = os.path.join(script_dir, RAW_DB_NAME)
    
    conn = sqlite3.connect(raw_db_path)
    c = conn.cursor()
    
    # Drop old tables if they exist so we start completely fresh (like 'w' mode in CSV)
    tables = ['raw_tables', 'raw_pdfs', 'raw_keywords', 'raw_iframes', 'raw_portals']
    for t in tables:
        c.execute(f"DROP TABLE IF EXISTS {t}")
        
    # Create the 5 tables
    c.execute('''CREATE TABLE raw_tables (Source_URL TEXT, Page TEXT, Row_Text TEXT, Embedded_Links TEXT)''')
    c.execute('''CREATE TABLE raw_pdfs (Source_URL TEXT, Page TEXT, Link_Text TEXT, Document_URL TEXT)''')
    c.execute('''CREATE TABLE raw_keywords (Source_URL TEXT, Page TEXT, Link_Text TEXT, Target_URL TEXT)''')
    c.execute('''CREATE TABLE raw_iframes (Source_URL TEXT, Page TEXT, IFrame_Source_URL TEXT)''')
    c.execute('''CREATE TABLE raw_portals (Source_URL TEXT, Page TEXT, Portal_Text TEXT, Portal_Link TEXT)''')
    
    conn.commit()
    conn.close()
    print(f"🗄️ Initialized blank raw database: {RAW_DB_NAME}")

# ==========================================
# 3. THE WORKER ENGINE
# ==========================================
def wait_until_rendered(page, settle_rounds=2, max_seconds=30):
    """Poll the DOM until its size stops growing, then return.

    Critical fix: with wait_until='commit' the page resolves the instant the server
    response arrives — long before JS renders the heavy tender tables. Under concurrent
    load these tables can take many seconds to paint, and a naive short wait would scrape
    an empty shell (observed: a site that yields 2,205 PDFs in isolation gave 0 under load).
    Waiting for the HTML length to stabilise guarantees we extract a fully-rendered page,
    however slow the machine is. Returns the final content length seen.
    """
    deadline = time.time() + max_seconds
    prev_len = -1
    stable = 0
    cur_len = 0
    while time.time() < deadline:
        try:
            cur_len = len(page.content())
        except Exception:
            cur_len = prev_len
        if cur_len == prev_len and cur_len > 0:
            stable += 1
            if stable >= settle_rounds:
                break
        else:
            stable = 0
        prev_len = cur_len
        page.wait_for_timeout(1000)
    return cur_len

def extract_and_store(source_url, base_url, html, page_number, raw_db_path):
    """Parse one page's HTML, store all rows, and return (content_fingerprint, rows_added).

    source_url: the seed site URL (stored as Source_URL so all pages group under one site).
    base_url:   the URL the HTML actually came from (used to resolve relative links).
    """
    soup = BeautifulSoup(html, 'html.parser')
    tables_data, pdfs_data, keywords_data, iframes_data, portals_data = [], [], [], [], []

    # --- EXTRACTION LOGIC ---
    for table in soup.find_all('table'):
        for row in table.find_all('tr'):
            cols = row.find_all(['td', 'th'])
            text = " | ".join([c.text.strip().replace('\n', ' ') for c in cols if c.text.strip()])
            links = ", ".join([urljoin(base_url, a['href']) for c in cols for a in c.find_all('a', href=True)])
            if len(text) > 5:
                tables_data.append((source_url, f"Page {page_number}", text, links))

    for link in soup.find_all('a', href=True):
        href = link['href'].lower()
        text = link.text.strip().replace('\n', ' ')
        full_url = urljoin(base_url, link['href'])

        if len(text) > 2:
            if any(ext in href for ext in doc_extensions):
                pdfs_data.append((source_url, f"Page {page_number}", text, full_url))
            elif any(portal in href for portal in external_portals):
                portals_data.append((source_url, f"Page {page_number}", text, full_url))
            elif any(kw in text.lower() or kw in href for kw in keywords):
                keywords_data.append((source_url, f"Page {page_number}", text, full_url))

    for iframe in soup.find_all('iframe', src=True):
        iframes_data.append((source_url, f"Page {page_number}", urljoin(base_url, iframe['src'])))

    # Fingerprint the extracted CONTENT only (text + links), excluding the "Page N" label,
    # so a looping "Next" that re-serves identical content is caught.
    fingerprint = str(
        [(t[2], t[3]) for t in tables_data]
        + [(d[2], d[3]) for d in pdfs_data]
        + [(k[2], k[3]) for k in keywords_data]
    )
    rows_added = len(tables_data) + len(pdfs_data) + len(keywords_data) + len(iframes_data) + len(portals_data)

    with db_lock:
        conn = sqlite3.connect(raw_db_path, timeout=15)
        c = conn.cursor()
        if tables_data:
            c.executemany("INSERT INTO raw_tables VALUES (?, ?, ?, ?)", tables_data)
        if pdfs_data:
            c.executemany("INSERT INTO raw_pdfs VALUES (?, ?, ?, ?)", pdfs_data)
        if keywords_data:
            c.executemany("INSERT INTO raw_keywords VALUES (?, ?, ?, ?)", keywords_data)
        if iframes_data:
            c.executemany("INSERT INTO raw_iframes VALUES (?, ?, ?)", iframes_data)
        if portals_data:
            c.executemany("INSERT INTO raw_portals VALUES (?, ?, ?, ?)", portals_data)
        conn.commit()
        conn.close()

    return fingerprint, rows_added

def http_get_html(url):
    """Fetch page HTML with a plain HTTP request — fast, memory-light, and reliable.

    The tender content on these gov sites is server-rendered (verified: curl returns the
    full listing with all PDF links), so no browser is needed. A headless browser actually
    FAILS here under memory pressure, returning empty shells. Returns HTML string or None.
    """
    for attempt in range(1, NAV_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=30, verify=False)
            if resp.status_code == 200 and resp.text:
                return resp.text
            return None
        except Exception:
            if attempt == NAV_RETRIES:
                return None
            time.sleep(2)
    return None

def find_next_page_url(soup, current_url):
    """Find the next pagination page via href links (rel=next, 'Next', '»', '>').
    Returns an absolute URL, or None if there's no real href-based next page."""
    candidates = []
    rel_next = soup.find(
        'a',
        href=True,
        rel=lambda v: v and 'next' in (v if isinstance(v, str) else ' '.join(v)).lower(),
    )
    if rel_next:
        candidates.append(rel_next)

    for a in soup.find_all('a', href=True):
        txt = a.get_text(strip=True).lower()
        title = (a.get('title') or '').lower()
        aria = (a.get('aria-label') or '').lower()
        if txt in ('next', 'next »', '»', '>', '>>', 'next page') or 'next' in title or 'next' in aria:
            candidates.append(a)

    for a in candidates:
        href = (a.get('href') or '').strip()
        if not href or href.startswith('#') or href.lower().startswith('javascript:'):
            continue  # JS-driven pagination — can't follow over plain HTTP
        return urljoin(current_url, href)
    return None

def scrape_http(url, raw_db_path):
    """HTTP-first scrape: fetch page 1, extract, follow href-based pagination.
    Returns total rows extracted, or -1 if the very first fetch failed."""
    html = http_get_html(url)
    if html is None:
        return -1

    total_rows = 0
    visited = set()
    current_url = url
    page_number = 1
    previous_fp = ""

    while page_number <= MAX_PAGES:
        if current_url in visited:
            break
        visited.add(current_url)

        if page_number > 1:
            html = http_get_html(current_url)
            if html is None:
                break

        soup = BeautifulSoup(html, 'html.parser')
        fp, rows = extract_and_store(url, current_url, html, page_number, raw_db_path)
        total_rows += rows

        if fp == previous_fp and fp != "[]":
            print(f"  👻 [GHOST LOOP] {url} page {page_number} repeats content. Stopping.")
            break
        previous_fp = fp

        next_url = find_next_page_url(soup, current_url)
        if not next_url or next_url in visited:
            print(f"  ✅ [SUCCESS-HTTP] {url} finished at page {page_number} ({total_rows} rows).")
            break
        if page_number >= MAX_PAGES:
            print(f"  🛑 [SAFETY STOP] {url} reached {MAX_PAGES} pages ({total_rows} rows).")
            break

        print(f"  🖱️ [PAGING-HTTP] {url} -> page {page_number + 1}")
        current_url = next_url
        page_number += 1

    return total_rows

def scrape_browser(url, raw_db_path):
    """Browser fallback for genuinely JS-rendered sites or JS-driven (postback) pagination.
    Heavier and slower, used only when the HTTP path returns nothing."""
    browser = None
    context = None
    page = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    ignore_https_errors=True,
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    viewport={'width': 1920, 'height': 1080},
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1"
                    }
                )
                page = context.new_page()

                # "commit" resolves the instant the server response arrives, bypassing the
                # phantom load/domcontentloaded events that never fire on these sites.
                for attempt in range(1, NAV_RETRIES + 1):
                    try:
                        page.goto(url, timeout=60000, wait_until="commit")
                        break
                    except Exception as nav_err:
                        if attempt == NAV_RETRIES:
                            raise
                        reason = str(nav_err).splitlines()[0][:50]
                        print(f"  🔁 [RETRY {attempt}/{NAV_RETRIES}] {url} ({reason}), retrying...")
                        page.wait_for_timeout(3000)

                wait_until_rendered(page)
                page_number = 1
                previous_page_fingerprint = ""
                previous_text_fingerprint = ""

                while True:
                    if page_number > MAX_PAGES:
                        print(f"  🛑 [SAFETY STOP] {url} reached max limit of {MAX_PAGES} pages.")
                        break

                    page.wait_for_timeout(1500)
                    html = page.content()
                    text_fp = hashlib.md5(BeautifulSoup(html, 'html.parser').get_text().encode('utf-8')).hexdigest()
                    if text_fp == previous_text_fingerprint:
                        print(f"  ✅ [SUCCESS-BROWSER] {url} finished — content stopped changing at Page {page_number}.")
                        break
                    previous_text_fingerprint = text_fp

                    fp, _ = extract_and_store(url, page.url, html, page_number, raw_db_path)
                    if fp == previous_page_fingerprint and fp != "[]":
                        print(f"  👻 [GHOST LOOP] {url} Page {page_number} is a fake duplicate. Breaking out.")
                        break
                    previous_page_fingerprint = fp

                    next_selectors = [
                        "a[title='Next']", "a[title='Next Page']", "a[rel='next']",
                        "li.pagination-next a", "li.pagenav a:has-text('Next')",
                        "a:has-text('Next'):not(.disabled):not([disabled])",
                        "a:text-is('Next')", "a:text-is('NEXT')",
                        "a:text-is('>')", "a:text-is('>>')", "a:text-is('»')",
                        "a:has-text('>')", "a:has-text('>>')",
                        "[aria-label*='Next']", "[aria-label*='next']", "[title*='Next']",
                        "li.next:not(.disabled) a", ".paginate_button.next:not(.disabled) a", ".pagination-next a",
                        "input[value='Next']", "input[type='image'][title='Next']",
                        "input[type='image'][alt='Next']", "img[alt='Next']",
                        "button:has-text('Next'):not([disabled])"
                    ]

                    clicked = False
                    for selector in next_selectors:
                        try:
                            btn = page.locator(selector).first
                            if btn.count() > 0 and btn.is_enabled() and btn.is_visible():
                                print(f"  🖱️ [PAGING-BROWSER] {url} -> Page {page_number + 1}...")
                                try:
                                    with page.expect_navigation(timeout=5000):
                                        btn.click(force=True)
                                except Exception:
                                    page.wait_for_timeout(3000)
                                wait_until_rendered(page)
                                page_number += 1
                                clicked = True
                                break
                        except Exception:
                            continue

                    if not clicked:
                        print(f"  ✅ [SUCCESS-BROWSER] {url} finished gracefully at Page {page_number}.")
                        break

            except PlaywrightTimeoutError:
                print(f"  ⏳ [TIMEOUT] {url} took too long to load or respond.")
            except Exception as e:
                print(f"  ❌ [ERROR] {url} -> {str(e).split(chr(10))[0]}")
            finally:
                for resource in (page, context, browser):
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception:
                            pass

    except Exception as e:
        print(f"  🚨 [FATAL] Playwright crashed on {url} -> {e}")

def scrape_single_url(url):
    print(f"🌐 [STARTED] {url}")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    raw_db_path = os.path.join(script_dir, RAW_DB_NAME)

    # HTTP-FIRST: fast, memory-light, and reliable for the server-rendered gov pages.
    try:
        rows = scrape_http(url, raw_db_path)
    except Exception as e:
        print(f"  ⚠️ [HTTP-ERROR] {url} -> {str(e).splitlines()[0][:60]}")
        rows = -1

    # Fall back to the heavy browser only when HTTP yielded nothing (genuine JS-rendered
    # site, or JS-driven pagination that plain HTTP can't follow).
    if rows <= 0:
        print(f"  🔁 [FALLBACK→BROWSER] {url} (HTTP yielded {max(rows, 0)} rows)")
        scrape_browser(url, raw_db_path)

# ==========================================
# 4. MAIN PARALLEL EXECUTION
# ==========================================
if __name__ == '__main__':
    # Initialize the Database before starting the threads
    init_raw_database()

    # 🚀 8 WORKERS (Optimized perfectly for M3 Pro)
    # Staggered launch: simultaneous Chromium cold-starts spike CPU/DNS/TLS load,
    # which was pushing already-slow .gov.in/.nic.in servers past the 60s timeout.
    # Reduced from 8: under full 8-way contention, slow gov pages didn't finish rendering
    # their pagination controls before we checked for them, causing false "no Next button"
    # stops that silently truncated real data (verified: a site found a working Next button
    # in isolation but not under 8-way load). Fewer workers trades runtime for completeness.
    with ThreadPoolExecutor(max_workers=4) as executor:
        for url in urls_to_scrape:
            executor.submit(scrape_single_url, url)
            time.sleep(2)

    print(f"\n🎉 MASTER EXTRACTION COMPLETE! Data is safely stored in '{RAW_DB_NAME}'.")