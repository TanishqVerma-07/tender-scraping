from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
import re
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

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
MAX_PAGES = 500  
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
def scrape_single_url(url):
    print(f"🌐 [STARTED] {url}")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    raw_db_path = os.path.join(script_dir, RAW_DB_NAME)
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            
            # 🔥 UPGRADED CONTEXT: Bypasses SSL errors and NIC firewalls
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

            try:
                page.goto(url, timeout=60000, wait_until="networkidle")
                page_number = 1
                previous_page_fingerprint = "" 
                
                while True:
                    if page_number > MAX_PAGES:
                        print(f"  🛑 [SAFETY STOP] {url} reached max limit of {MAX_PAGES} pages.")
                        break

                    page.wait_for_timeout(1500)
                    soup = BeautifulSoup(page.content(), 'html.parser')
                    
                    tables_data, pdfs_data, keywords_data, iframes_data, portals_data = [], [], [], [], []
                    
                    # --- EXTRACTION LOGIC ---
                    for table in soup.find_all('table'):
                        for row in table.find_all('tr'):
                            cols = row.find_all(['td', 'th'])
                            text = " | ".join([c.text.strip().replace('\n', ' ') for c in cols if c.text.strip()])
                            links = ", ".join([urljoin(url, a['href']) for c in cols for a in c.find_all('a', href=True)])
                            if len(text) > 5: 
                                tables_data.append((url, f"Page {page_number}", text, links))
                    
                    for link in soup.find_all('a', href=True):
                        href = link['href'].lower()
                        text = link.text.strip().replace('\n', ' ')
                        full_url = urljoin(url, link['href'])
                        
                        if len(text) > 2:
                            if any(ext in href for ext in doc_extensions):
                                pdfs_data.append((url, f"Page {page_number}", text, full_url))
                            elif any(portal in href for portal in external_portals):
                                portals_data.append((url, f"Page {page_number}", text, full_url))
                            elif any(kw in text.lower() or kw in href for kw in keywords):
                                keywords_data.append((url, f"Page {page_number}", text, full_url))
                                
                    for iframe in soup.find_all('iframe', src=True):
                        iframes_data.append((url, f"Page {page_number}", urljoin(url, iframe['src'])))
                        
                    # SAFETY 2: GHOST LOOP DETECTION
                    current_page_fingerprint = str(tables_data) + str(pdfs_data) + str(keywords_data)
                    
                    if current_page_fingerprint == previous_page_fingerprint and current_page_fingerprint != "":
                        print(f"  👻 [GHOST LOOP] {url} Page {page_number} is a fake duplicate. Breaking out.")
                        break
                    
                    previous_page_fingerprint = current_page_fingerprint
                        
                    # ==========================================
                    # 🛑 SECURE DATABASE WRITE (Thread Safe)
                    # ==========================================
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
                        
                    # ==========================================
                    # 🎯 PAGINATION ENGINE (UPGRADED FOR MP MANDI & JOOMLA)
                    # ==========================================
                    next_selectors = [
                        # MP Mandi / Joomla specific selectors
                        "a[title='Next']", "a[title='Next Page']", "a[rel='next']",
                        "li.pagination-next a", "li.pagenav a:has-text('Next')",
                        
                        # 1. Standard Text buttons
                        "a:has-text('Next'):not(.disabled):not([disabled])", 
                        "a:text-is('Next')", "a:text-is('NEXT')",
                        
                        # 2. Arrows (Heavily used in ASP.NET GridViews)
                        "a:text-is('>')", "a:text-is('>>')", "a:text-is('»')",
                        "a:has-text('>')", "a:has-text('>>')",
                        
                        # 3. Accessibility labels (Modern React/Angular SPAs)
                        "[aria-label*='Next']", "[aria-label*='next']", "[title*='Next']",
                        
                        # 4. Standard classes
                        "li.next:not(.disabled) a", ".paginate_button.next:not(.disabled) a", ".pagination-next a",
                        
                        # 5. NICGEP Image Buttons & Form Inputs
                        "input[value='Next']", "input[type='image'][title='Next']", 
                        "input[type='image'][alt='Next']", "img[alt='Next']", 
                        "button:has-text('Next'):not([disabled])"
                    ]
                    
                    clicked = False
                    for selector in next_selectors:
                        try:
                            # Search for the button
                            btn = page.locator(selector).first
                            
                            if btn.count() > 0 and btn.is_enabled() and btn.is_visible():
                                print(f"  🖱️ [PAGING] {url} -> Page {page_number + 1}...")
                                
                                # THE FIX: We try to catch the "Hard Reload" that Joomla uses.
                                # If it's a hard reload, we wait for it. If it's AJAX, the timeout safely catches it.
                                try:
                                    with page.expect_navigation(timeout=5000):
                                        btn.click(force=True)
                                except Exception:
                                    # If expect_navigation timed out, it means it was a soft AJAX load.
                                    # We wait a moment for the JSON to render the new table.
                                    page.wait_for_timeout(3000)
                                
                                # Extra safety buffer for slow Indian Gov servers like MP Mandi
                                page.wait_for_timeout(1000)
                                
                                page_number += 1
                                clicked = True
                                break
                        except Exception:
                            # If a specific locator throws an error, ignore it and try the next one
                            continue
                            
                    if not clicked:
                        print(f"  ✅ [SUCCESS] {url} finished gracefully at Page {page_number}.")
                        break 

            except PlaywrightTimeoutError:
                print(f"  ⏳ [TIMEOUT] {url} took too long to load or respond.")
            except Exception as e:
                error_message = str(e).split('\n')[0] 
                print(f"  ❌ [ERROR] {url} -> {error_message}")
            finally:
                page.close()
                context.close()
                browser.close()
                
    except Exception as e:
        print(f"  🚨 [FATAL] Playwright crashed on {url} -> {e}")

# ==========================================
# 4. MAIN PARALLEL EXECUTION
# ==========================================
if __name__ == '__main__':
    # Initialize the Database before starting the threads
    init_raw_database()

    # 🚀 8 WORKERS (Optimized perfectly for M3 Pro)
    with ThreadPoolExecutor(max_workers=8) as executor:
        for url in urls_to_scrape:
            executor.submit(scrape_single_url, url)

    print(f"\n🎉 MASTER EXTRACTION COMPLETE! Data is safely stored in '{RAW_DB_NAME}'.")