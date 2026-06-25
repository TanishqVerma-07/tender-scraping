import streamlit as st
import sqlite3
from datetime import date
import json
import os
import subprocess

# ==========================================
# 0. PLAYWRIGHT BROWSER SETUP (first run only)
# ==========================================
# Hosting platforms like Streamlit Cloud have no generic "run this after pip install"
# hook, so the Chromium binary scraper.py's browser fallback needs has to be installed
# from code. Cached after the first run via a marker file (re-installing every page load
# would be slow and pointless).
_PLAYWRIGHT_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".playwright_installed")
if not os.path.exists(_PLAYWRIGHT_MARKER):
    subprocess.run(["playwright", "install", "chromium"], check=False)
    open(_PLAYWRIGHT_MARKER, "w").close()

# ==========================================
# 1. PAGE CONFIGURATION & GLOBALS
# ==========================================
st.set_page_config(page_title="Upjao AI Tender", page_icon=":material/policy:", layout="wide")
today_str = date.today().strftime("%B %d, %Y")

# ==========================================
# 2. DESIGN SYSTEM (Flat / Enterprise SaaS)
# ==========================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;1,400&display=swap');

html, body, [class*="css"]  {
    font-family: 'Plus Jakarta Sans', sans-serif;
}

@media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
}

.upjao-card {
    background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    height: 100%;
    transition: border-color 180ms ease;
}
.upjao-card:hover {
    border-color: #0369A1;
}
.upjao-card h4 {
    margin: 0 0 0.35rem 0;
    color: #0F172A;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.upjao-card p {
    margin: 0 0 0.9rem 0;
    color: #334155;
    font-size: 0.9rem;
    line-height: 1.5;
}
.upjao-badge {
    display: inline-block;
    background-color: #E2E8F0;
    color: #0F172A;
    border-radius: 999px;
    padding: 0.2rem 0.85rem;
    font-size: 0.8rem;
    font-weight: 600;
    margin-top: 0.4rem;
}
.upjao-badge-ok {
    background-color: #DCFCE7;
    color: #166534;
}
.upjao-badge-warn {
    background-color: #FEF3C7;
    color: #92400E;
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2b. .ENV HELPERS (Vertex AI credentials)
# ==========================================
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
VERTEX_PROJECT_PLACEHOLDER = "add your project id here"

def read_env_value(key, default=""):
    if not os.path.exists(ENV_PATH):
        return default
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return default

def update_env_values(updates):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    keys_found = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            k = stripped.split('=', 1)[0].strip()
            if k in updates:
                new_lines.append(f'{k}="{updates[k]}"\n')
                keys_found.add(k)
                continue
        new_lines.append(line)

    for k, v in updates.items():
        if k not in keys_found:
            new_lines.append(f'{k}="{v}"\n')

    with open(ENV_PATH, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

def is_vertex_configured():
    project_id = read_env_value("GOOGLE_CLOUD_PROJECT")
    return bool(project_id) and project_id != VERTEX_PROJECT_PLACEHOLDER

def get_email_recipients():
    raw = read_env_value("EMAIL_RECIPIENT")
    return [r.strip() for r in raw.split(',') if r.strip()]

def is_email_configured():
    sender = read_env_value("EMAIL_SENDER")
    password = read_env_value("EMAIL_PASSWORD")
    return bool(sender) and bool(password) and bool(get_email_recipients())

# ==========================================
# 2c. GITHUB AUTO-SYNC
# ==========================================
# tenders.db edited here (websites/keywords/blacklist/prompts) lives wherever this
# dashboard happens to be running. The daily GitHub Actions pipeline checks out a FRESH
# copy from GitHub every run, so it never sees a change made here unless we push it back
# to the repo ourselves, immediately after every save.
GITHUB_REPO = "TanishqVerma-07/tender-scraping"

def get_github_token():
    # Streamlit Cloud secrets take priority (st.secrets); falls back to .env for local runs.
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
        if token:
            return token
    except Exception:
        pass
    return read_env_value("GITHUB_TOKEN")

def is_github_sync_configured():
    return bool(get_github_token())

def sync_tenders_db_to_github():
    """Best-effort: commit + push tenders.db to GitHub so tomorrow's automated
    pipeline run picks up dashboard edits. Failures are shown as a warning, not a
    crash — the local save to tenders.db already succeeded regardless of this."""
    token = get_github_token()
    if not token:
        st.warning("Saved locally, but GitHub sync isn't configured — this change won't reach "
                    "tomorrow's automated run until GITHUB_TOKEN is set. See Settings.")
        return

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    push_url = f"https://x-access-token:{token}@github.com/{GITHUB_REPO}.git"

    def run_git(*args):
        return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True)

    try:
        run_git("config", "user.email", "dashboard@upjao.ai")
        run_git("config", "user.name", "Upjao Dashboard")

        diff = run_git("status", "--porcelain", "tenders.db")
        if not diff.stdout.strip():
            return  # nothing changed (e.g. identical overwrite)

        run_git("add", "tenders.db")
        run_git("commit", "-m", "Update tenders.db via dashboard")

        # Pull first in case GitHub Actions pushed a commit since this container last synced.
        pull = run_git("pull", "--rebase", "--autostash", push_url, "main")
        if pull.returncode != 0:
            st.warning(f"Saved locally, but GitHub sync hit a conflict pulling remote changes: "
                       f"{pull.stderr.strip()[:200]}. Resolve manually or contact an admin.")
            return

        push = run_git("push", push_url, "main")
        if push.returncode != 0:
            st.warning(f"Saved locally, but pushing to GitHub failed: {push.stderr.strip()[:200]}")
        else:
            st.toast("Synced to GitHub — tomorrow's automated run will use this.", icon="✅")
    except Exception as e:
        st.warning(f"Saved locally, but GitHub sync failed unexpectedly: {e}")

# ==========================================
# 3. DATABASE INITIALIZATION
# ==========================================
def init_db():
    conn = sqlite3.connect('tenders.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS websites (id INTEGER PRIMARY KEY, item TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS keywords (id INTEGER PRIMARY KEY, item TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (id INTEGER PRIMARY KEY, item TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS prompts (id INTEGER PRIMARY KEY, item TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS past_tenders (id INTEGER PRIMARY KEY, item TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS past_canceled_tenders (id INTEGER PRIMARY KEY, item TEXT UNIQUE)''')
    conn.commit()
    conn.close()

init_db()

# ==========================================
# 4. HELPER FUNCTIONS
# ==========================================
def get_items(table):
    conn = sqlite3.connect('tenders.db')
    c = conn.cursor()
    c.execute(f"SELECT item FROM {table}")
    items = [row[0] for row in c.fetchall()]
    conn.close()
    return items

def export_all_json():
    # Backs up your configuration to JSON files safely
    for table, key in [('websites', 'url'), ('keywords', 'keyword'), ('blacklist', 'blacklist_word'), ('prompts', 'prompt'), ('past_tenders', 'tender'), ('past_canceled_tenders', 'canceled_tender')]:
        with open(f"{table}.json", "w", encoding='utf-8') as f:
            json.dump([{key: i} for i in get_items(table)], f, ensure_ascii=False, indent=4)

def overwrite_items(table, items_list):
    conn = sqlite3.connect('tenders.db')
    c = conn.cursor()
    c.execute(f"DELETE FROM {table}")
    for item in items_list:
        clean_item = item.strip()
        if clean_item:
            try:
                c.execute(f"INSERT INTO {table} (item) VALUES (?)", (clean_item,))
            except sqlite3.IntegrityError:
                pass
    conn.commit()
    conn.close()
    export_all_json()
    sync_tenders_db_to_github()

# ==========================================
# 5. DIALOG CANVASES
# ==========================================
@st.dialog("Edit Websites", width="large")
def websites_canvas():
    st.caption("One URL per line. Saved instantly to the scraper's source list.")
    new_text = st.text_area("Websites", value="\n".join(get_items('websites')), height=300, label_visibility="collapsed")
    if st.button("Save changes", type="primary", use_container_width=True):
        overwrite_items('websites', new_text.split('\n')); st.rerun()

@st.dialog("Edit Target Keywords", width="large")
def keywords_canvas():
    st.caption("One plain word or phrase per line (no regex needed). Rows containing any of these are kept.")
    new_text = st.text_area("Keywords", value="\n".join(get_items('keywords')), height=300, label_visibility="collapsed")
    if st.button("Save changes", type="primary", use_container_width=True):
        overwrite_items('keywords', new_text.split('\n')); st.rerun()

@st.dialog("Edit Garbage Blacklist", width="large")
def blacklist_canvas():
    st.caption("One plain word or phrase per line (no regex needed). Rows containing any of these as a whole word are discarded.")
    new_text = st.text_area("Blacklist Words", value="\n".join(get_items('blacklist')), height=300, label_visibility="collapsed")
    if st.button("Save changes", type="primary", use_container_width=True):
        overwrite_items('blacklist', new_text.split('\n')); st.rerun()

@st.dialog("Connect Vertex AI", width="large")
def vertex_setup_canvas():
    st.caption("The AI Evaluator (ai.py) uses Google Vertex AI / Gemini. Enter your Google Cloud project to enable it — saved to .env.")

    current_project = read_env_value("GOOGLE_CLOUD_PROJECT")
    if current_project == VERTEX_PROJECT_PLACEHOLDER:
        current_project = ""
    current_location = read_env_value("GOOGLE_CLOUD_LOCATION", "us-central1")

    project_id = st.text_input("Google Cloud project ID", value=current_project, placeholder="my-gcp-project-id")
    location = st.text_input("Vertex AI location", value=current_location, placeholder="us-central1")

    st.info("Also run `gcloud auth application-default login` on this machine so ai.py can authenticate to Vertex AI.")

    if st.button("Save & connect", type="primary", use_container_width=True):
        if not project_id.strip():
            st.error("Project ID is required.")
        else:
            update_env_values({
                "GOOGLE_CLOUD_PROJECT": project_id.strip(),
                "GOOGLE_CLOUD_LOCATION": location.strip() or "us-central1",
            })
            st.success("Vertex AI credentials saved to .env")
            st.rerun()

@st.dialog("Email Notifications", width="large")
def email_setup_canvas():
    st.caption("Reports (UPJAO_MASTER_LEADS.csv and, if AI is enabled, UPJAO_AI_SALES_STRATEGY.csv) "
               "are emailed via Gmail after each pipeline run — saved to .env.")

    current_sender = read_env_value("EMAIL_SENDER")
    current_password = read_env_value("EMAIL_PASSWORD")
    current_recipients = get_email_recipients()

    sender = st.text_input("Sender Gmail address", value=current_sender, placeholder="you@gmail.com")
    password = st.text_input("Gmail app password", value=current_password, type="password", placeholder="16-character app password")
    recipients_text = st.text_area(
        "Recipients (one email per line)",
        value="\n".join(current_recipients),
        height=150,
        placeholder="person1@example.com\nperson2@example.com",
    )

    st.info("Use a Gmail **App Password** (Google Account → Security → App passwords), not the "
            "account's normal login password. Add one recipient per line to send to multiple people.")

    if st.button("Save email settings", type="primary", use_container_width=True):
        new_recipients = [r.strip() for r in recipients_text.split('\n') if r.strip()]
        if not sender.strip() or not password.strip():
            st.error("Sender email and app password are required.")
        elif not new_recipients:
            st.error("Add at least one recipient.")
        else:
            update_env_values({
                "EMAIL_SENDER": sender.strip(),
                "EMAIL_PASSWORD": password.strip(),
                "EMAIL_RECIPIENT": ", ".join(new_recipients),
            })
            st.success("Email settings saved to .env")
            st.rerun()

# ==========================================
# 6. HEADER
# ==========================================
st.title(":material/policy: Upjao AI Tender Command Center")
st.caption(f"Configuration & control panel — {today_str}")
st.divider()

# ==========================================
# 7. PIPELINE ENGINES
# ==========================================
st.subheader(":material/play_circle: Pipeline Engines")
st.caption("Each stage runs in the background; check the terminal for live progress.")

eng_col1, eng_col2, eng_col3 = st.columns(3, gap="medium")

with eng_col1:
    with st.container(border=True):
        st.markdown("##### :material/travel_explore: Web Scraper")
        st.caption("Crawl saved websites for tenders, tables, PDFs, and notices.")
        if st.button("Start scraper", use_container_width=True, type="primary", help="Runs scraper.py in the background"):
            try:
                subprocess.Popen(["python", "scraper.py"])
                st.success("Scraper started.")
            except Exception as e:
                st.error(f"Failed to start scraper: {e}")

with eng_col2:
    with st.container(border=True):
        st.markdown("##### :material/filter_alt: Keyword Cleaner")
        st.caption("Apply keyword and blacklist filters to raw scrape results.")
        if st.button("Run cleaner", use_container_width=True, type="primary", help="Runs clean.py in the background"):
            try:
                subprocess.Popen(["python", "clean.py"])
                st.success("Cleaner started.")
            except Exception as e:
                st.error(f"Failed: {e}")

with eng_col3:
    with st.container(border=True):
        st.markdown("##### :material/smart_toy: AI Evaluator")
        st.caption("Score cleaned leads and generate a sales strategy per tender.")
        if is_vertex_configured():
            st.markdown(f'<span class="upjao-badge upjao-badge-ok">Connected: {read_env_value("GOOGLE_CLOUD_PROJECT")}</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="upjao-badge upjao-badge-warn">Vertex AI not connected</span>', unsafe_allow_html=True)
        st.write("")
        if st.button("Run AI evaluator", use_container_width=True, type="primary", help="Runs ai.py in the background"):
            if not is_vertex_configured():
                vertex_setup_canvas()
            else:
                try:
                    subprocess.Popen(["python", "ai.py"])
                    st.success("AI evaluation started.")
                except Exception as e:
                    st.error(f"Failed: {e}")

st.divider()

# ==========================================
# 8. DATA SOURCES & FILTERS
# ==========================================
st.subheader(":material/tune: Data Sources & Filters")
st.caption("These lists drive the scraper and cleaner stages.")

src_col1, src_col2, src_col3 = st.columns(3, gap="medium")

with src_col1:
    with st.container(border=True):
        st.markdown("##### :material/language: Websites")
        st.caption("Target sites the scraper will visit.")
        st.markdown(f'<span class="upjao-badge">{len(get_items("websites"))} saved</span>', unsafe_allow_html=True)
        st.write("")
        if st.button("Edit websites", use_container_width=True):
            websites_canvas()

with src_col2:
    with st.container(border=True):
        st.markdown("##### :material/key: Target Keywords")
        st.caption("Whitelist — leads must match at least one of these.")
        st.markdown(f'<span class="upjao-badge">{len(get_items("keywords"))} saved</span>', unsafe_allow_html=True)
        st.write("")
        if st.button("Edit keywords", use_container_width=True):
            keywords_canvas()

with src_col3:
    with st.container(border=True):
        st.markdown("##### :material/block: Garbage Blacklist")
        st.caption("Blacklist — leads matching these terms are discarded.")
        st.markdown(f'<span class="upjao-badge">{len(get_items("blacklist"))} saved</span>', unsafe_allow_html=True)
        st.write("")
        if st.button("Edit blacklist", use_container_width=True):
            blacklist_canvas()

st.divider()

# ==========================================
# 8b. EMAIL DELIVERY
# ==========================================
st.subheader(":material/mail: Email Delivery")
st.caption("Reports are emailed to these recipients after each pipeline run.")

with st.container(border=True):
    email_col1, email_col2 = st.columns([3, 1])
    with email_col1:
        st.markdown("##### :material/forward_to_inbox: Email Notifications")
        if is_email_configured():
            recipients = get_email_recipients()
            st.markdown(
                f'<span class="upjao-badge upjao-badge-ok">Sending from {read_env_value("EMAIL_SENDER")} '
                f'to {len(recipients)} recipient(s)</span>',
                unsafe_allow_html=True,
            )
            st.caption(", ".join(recipients))
        else:
            st.markdown('<span class="upjao-badge upjao-badge-warn">Email not configured</span>', unsafe_allow_html=True)
    with email_col2:
        st.write("")
        if st.button("Edit email settings", use_container_width=True):
            email_setup_canvas()

st.divider()

# ==========================================
# 8c. GITHUB SYNC STATUS
# ==========================================
st.subheader(":material/sync: GitHub Sync")
st.caption("Website/keyword/blacklist edits made here are pushed to GitHub automatically so "
           "tomorrow's automated daily run picks them up.")

with st.container(border=True):
    if is_github_sync_configured():
        st.markdown(f'<span class="upjao-badge upjao-badge-ok">Connected to {GITHUB_REPO}</span>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<span class="upjao-badge upjao-badge-warn">Not configured</span>', unsafe_allow_html=True)
        st.caption("Set a `GITHUB_TOKEN` repo secret (Streamlit Cloud → Settings → Secrets) "
                   "or in `.env` locally, with permission to push to this repo.")

st.divider()

# ==========================================
# 9. AI STRATEGY PROMPT
# ==========================================
st.subheader(":material/psychology: AI Strategy Prompt")
st.caption("Defines how Gemini evaluates and scores each lead. Changes apply on the next AI run.")

with st.container(border=True):
    new_prompt = st.text_area("Prompt rules", value="\n".join(get_items('prompts')), height=220, label_visibility="collapsed")
    if st.button("Save prompt", type="primary"):
        overwrite_items('prompts', [new_prompt])
        st.success("Prompt saved.")
        st.rerun()

st.divider()

# ==========================================
# 10. IMPORT EXTERNAL DATA
# ==========================================
st.subheader(":material/upload_file: Import External Data")
st.caption("Upload raw tender exports (e.g. from TenderTiger) for inclusion in the pipeline.")

with st.container(border=True):
    uploaded_file = st.file_uploader("Upload .xlsx or .xls file", type=["xlsx", "xls"], label_visibility="collapsed")
    if uploaded_file is not None:
        if st.button("Save file to system", type="primary"):
            with open(uploaded_file.name, "wb") as f:
                f.write(uploaded_file.getbuffer())
            st.success(f"Saved '{uploaded_file.name}'.")
