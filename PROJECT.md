# Upjao AI Tender Pipeline — Project Reference

This document describes the **active pipeline** in this project: what each file does, what data
flows between them, and how everything fits together. It is written so an AI assistant (or a new
developer) can understand the whole system without having to read every file.

The project's purpose: find government/business tender notices on a list of websites, filter them
down to ones relevant to **Upjao Agrotech** (a company selling AI grain analyzers, moisture meters,
and digital weighing scales), optionally run them through Gemini for a sales-strategy writeup, and
email the results.

---

## 1. High-Level Pipeline

```
tenders.db (config)
     │
     ▼
scraper.py  ──scrape──▶  raw_database.db   (wiped & rebuilt every run)
     │
     ▼
clean.py  ──filter + dedup vs history──▶  upjao_cleansed_database.db  (cumulative, append-only)
     │                                          │
     ▼                                          ▼
UPJAO_MASTER_LEADS.csv                 (history of every lead ever seen)
 (ONLY today's NEW leads)
     │
     ├──▶ send_email.py  ──▶  emails "Validated Sales Leads Report"
     │
     ▼ (only if Vertex AI configured)
ai.py  ──Gemini 2.5 Pro──▶  UPJAO_AI_SALES_STRATEGY.csv  (only today's new leads, AI-scored)
     │
     ▼
send_email.py  ──▶  emails "AI Sales Strategy Report"
```

All of the above is orchestrated by **`pipeline.py`**, which runs each step as a subprocess in
order and stops if any step fails.

`app.py` is a separate **Streamlit configuration UI** that edits `tenders.db` (the config database)
and can also manually trigger each stage.

---

## 2. File-by-File Reference

### `pipeline.py` — Orchestrator
Runs the full daily pipeline, in order, each as a subprocess (`python <script>.py`) using the same
Python interpreter that invoked it:

1. **`scraper.py`** — scrape websites → `raw_database.db`
2. **`clean.py`** — filter/dedupe → `upjao_cleansed_database.db` + `UPJAO_MASTER_LEADS.csv`
3. **`send_email.py UPJAO_MASTER_LEADS.csv`** — email the cleaned leads
4. **`ai.py`** (skipped if Vertex AI isn't configured — see §5) → `UPJAO_AI_SALES_STRATEGY.csv`
5. **`send_email.py UPJAO_AI_SALES_STRATEGY.csv`** — email the AI strategy report (only if step 4 ran)

- Loads `.env` at startup via `python-dotenv`.
- The Vertex AI "configured?" check: reads `GOOGLE_CLOUD_PROJECT` from `.env`. If it equals the
  literal placeholder string `"add your project id here"`, steps 4 and 5 are skipped and a message
  is printed.
- Any non-zero exit code from a step aborts the whole pipeline (`sys.exit`).
- Intended to run once daily (e.g. via cron at 7 AM) on a host where `tenders.db`,
  `upjao_cleansed_database.db`, and `.env` **persist between runs** — losing
  `upjao_cleansed_database.db` resets the "already sent" history and everything looks new again.

### `scraper.py` — Scraper (Stage 1)
- **Input:** `tenders.db`, table `websites` (column `item`), read-only. Only rows starting with
  `http` are used; direct `.pdf` URLs are skipped. Duplicates removed.
- **Output:** `raw_database.db` — **wiped and recreated from scratch every run**
  (`init_raw_database()` does `DROP TABLE IF EXISTS` + `CREATE TABLE` for all 5 tables before
  scraping starts).
- **Process:**
  - Uses Playwright (Chromium, headless) with `ignore_https_errors=True` and a spoofed
    desktop Chrome user-agent (to bypass NIC/government-site SSL issues and basic firewalls).
  - 8 parallel worker threads (`ThreadPoolExecutor(max_workers=8)`), one URL per worker at a time.
  - For each URL, loads the page, then for up to `MAX_PAGES = 500` pages:
    - Parses HTML with BeautifulSoup.
    - Extracts:
      - **Tables**: every `<table>` row → `raw_tables (Source_URL, Page, Row_Text, Embedded_Links)`
      - **Documents**: links ending in `.pdf/.doc/.docx/.xls/.xlsx/.zip` →
        `raw_pdfs (Source_URL, Page, Link_Text, Document_URL)`
      - **Keyword links**: `<a>` tags whose text/href contains `tender, notice, corrigendum, rfp,
        rfq, auction, bid, eoi` → `raw_keywords (Source_URL, Page, Link_Text, Target_URL)`
      - **External portals**: links containing `eprocure, gem.gov.in, tenderwizard` →
        `raw_portals (Source_URL, Page, Portal_Text, Portal_Link)`
      - **Iframes**: `<iframe src="...">` → `raw_iframes (Source_URL, Page, IFrame_Source_URL)`
    - Writes each page's extracted rows to `raw_database.db` immediately (thread-safe via a
      global `db_lock`).
    - Tries ~20 different "Next page" selectors (covers ASP.NET GridViews, Joomla, NICGEP,
      React/Angular SPAs, image buttons, etc.) to paginate.
    - Stops on: no "Next" button found, `MAX_PAGES` reached, or a "ghost loop" (next page's
      extracted content is byte-identical to the previous page — detected via a fingerprint of
      `tables_data + pdfs_data + keywords_data`).
  - Errors per-URL (timeouts, crashes) are caught and logged; one bad site doesn't stop others.

### `clean.py` — Cleaner / Deduper (Stage 2)
- **Inputs:**
  - `raw_database.db` (from `scraper.py` — today's full fresh scrape)
  - `tenders.db`, table `keywords` (column `item`) — the **whitelist**, read via
    `get_ui_keywords()`. Users type **plain words/phrases** (no regex needed) — each is
    `re.escape()`'d and used as a **substring match**. Substring matching is intentional: stems
    like `grain analy` must keep matching "Grain **Analy**zer" / "Grain **Analy**sis" / "Grain
    **Analy**tics". If the whitelist ends up empty, the script aborts with an error (won't process
    anything with zero keywords).
  - `tenders.db`, table `blacklist` (column `item`) — the **blacklist**, read via
    `get_ui_blacklist()`. Users type **plain words/phrases** — each is `re.escape()`'d and wrapped
    in `\bterm\b` (whole-word match), so e.g. `tph`/`road`/`jcb` reject "5 tph plant" / "road
    repair" but do NOT false-trigger on "depth", "crossroads", etc. Pre-seeded with ~51 terms for
    irrelevant tenders (roads, hospitals, tractors, JCBs, catering, security guards, etc.), in
    English + several Indian regional languages (Hindi, Gujarati, Bengali, Odia, Punjabi). Editable
    live via `app.py`'s "Garbage Blacklist" card. If the blacklist is empty, no rows are
    blacklisted (nothing is rejected on that basis).
  - Both lists are loaded via the shared helper `_load_terms(table_name, whole_word=...)` in
    `clean.py` — `whole_word=False` for keywords (substring), `whole_word=True` for blacklist
    (whole-word). **Users never write regex** — plain text only.
- **Output:**
  - `upjao_cleansed_database.db` — **cumulative, append-only**. Same 5 tables as `raw_database.db`.
  - `UPJAO_MASTER_LEADS.csv` — derived from the `raw_tables` table only.
- **Process, per table** (`raw_tables`, `raw_pdfs`, `raw_keywords`, `raw_iframes`, `raw_portals`):
  1. Load all rows from `raw_database.db`.
  2. Drop exact-duplicate rows (ignoring the `Page` column, since the same row can appear on
     different page numbers across runs).
  3. Concatenate all columns per row into one lowercase string; keep the row only if it matches
     the whitelist regex **and does not** match the blacklist regex.
     → these are today's "survivors" (`survivors_df`).
  4. **Dedup against history**: load the existing rows for this table from
     `upjao_cleansed_database.db` (ignoring `Page`). Any survivor row whose
     (all-columns-except-Page) combination already exists in the cumulative DB is considered
     "already sent before" and dropped. What's left is `new_df` — genuinely new leads.
  5. Append `new_df` to `upjao_cleansed_database.db` (`if_exists='append'`). If the table doesn't
     exist yet, it's created.
  6. For `raw_tables` only: write `new_df` (NOT `survivors_df`) to `UPJAO_MASTER_LEADS.csv`, using
     `utf-8-sig` encoding so Excel renders Hindi/regional text correctly. If there are zero new
     rows, an empty (header-only) CSV is written.
- **Net effect:** `UPJAO_MASTER_LEADS.csv` always contains *only the tenders discovered for the
  first time on this run* — already-reported tenders are never repeated in the CSV/email, but they
  remain permanently in `upjao_cleansed_database.db` as the historical record.

### `ai.py` — AI Evaluator (Stage 4, optional)
- **Inputs:**
  - `UPJAO_MASTER_LEADS.csv` (today's new leads only, from `clean.py`)
  - `tenders.db`:
    - table `prompts` (first row) — the system prompt / persona for Gemini. Falls back to a
      hardcoded Upjao sales-director prompt if the table is empty.
    - table `keywords` — appended to the prompt as "target keywords to look for".
    - table `past_tenders` — appended as "examples of past winning tenders" (few-shot examples).
  - `.env`:
    - `GOOGLE_CLOUD_PROJECT` (**required**, no default — raises `RuntimeError` if missing)
    - `GOOGLE_CLOUD_LOCATION` (**required**, no default)
  - Vertex AI auth via the machine's **Application Default Credentials**
    (`~/.config/gcloud/application_default_credentials.json`, set up via
    `gcloud auth application-default login`) — not stored in this repo.
- **Output:** `UPJAO_AI_SALES_STRATEGY.csv` with columns:
  `Tender Name, Description, Size Value, Size Text, Deadline, PDF Link, Source Type,
  AI Match Reason, Upjao Strategy Mapping`
- **Process:**
  1. Builds a dynamic system prompt (base prompt + today's date + keyword list + past-winner
     examples). Only tenders closing in the current year or later (or "Not specified") are
     accepted.
  2. Reads `UPJAO_MASTER_LEADS.csv`, converts each row to a `"col: value | col: value..."` string.
  3. Splits rows into chunks of `CHUNK_SIZE = 20`.
  4. Runs up to `AI_WORKERS = 3` chunks in parallel (1s stagger between submissions to avoid rate
     limits) via `ThreadPoolExecutor`.
  5. Each chunk → one call to `gemini-2.5-pro` (Vertex AI) with `response_schema=TenderList`
     (Pydantic schema: `Tender` has `name, description, size_value, size_text, deadline_date,
     pdf_link, data_source_type, match_reason, actionable_strategy`), `temperature=0.0`.
  6. Results are appended to the output CSV thread-safely (`ai_csv_lock`) as each chunk finishes.

### `send_email.py` — Emailer (Stages 3 & 5)
- **Usage:** `python send_email.py [csv_file]` — `csv_file` defaults to `UPJAO_MASTER_LEADS.csv`.
- **Credentials:** manually parsed from `.env` (not via `python-dotenv`) — looks for
  `EMAIL_SENDER`/`EMAIL_USER`, `EMAIL_PASSWORD`, `EMAIL_RECIPIENT`. Exits if `.env` is missing or
  credentials can't be found.
- **Multiple recipients:** `EMAIL_RECIPIENT` may be a **comma-separated list**
  (e.g. `"a@gmail.com, b@company.com"`). The script splits it into `RECIPIENT_LIST` and sends one
  email with all of them in the `To` header (`smtplib`'s `send_message` delivers to every address
  in `To`). Exits with an error if the list is empty.
- **Process:**
  - Reads the given CSV, builds an HTML table (styled report). Any cell containing `http` is
    rendered as a "View Document 📄" link (only the first comma-separated URL in the cell).
  - If the CSV has zero data rows, prints a warning and **exits without sending** (no empty-email
    spam on quiet days).
  - Sends via Gmail SMTP (`smtp.gmail.com:587`, STARTTLS) using `EMAIL_SENDER`/`EMAIL_PASSWORD`
    (a Gmail **app password**, not the account password) to every address in `EMAIL_RECIPIENT`.
  - Report title / email subject is **"AI Sales Strategy Report"** if the input file is
    `UPJAO_AI_SALES_STRATEGY.csv`, otherwise **"Validated Sales Leads Report"**.

### `app.py` — Configuration Dashboard (Streamlit)
A **configuration-only** UI (no tender review/feed — that was removed). Run with:
```bash
streamlit run app.py
```
Sections:
- **Pipeline Engines** (3 cards): buttons to launch `scraper.py`, `clean.py`,
  `ai.py` as background subprocesses (`subprocess.Popen`).
  - The AI Evaluator card shows a connection badge:
    - Green **"Connected: `<project_id>`"** if `.env`'s `GOOGLE_CLOUD_PROJECT` is set to a real
      (non-placeholder) value.
    - Amber **"Vertex AI not connected"** otherwise.
  - Clicking "Run AI evaluator" while unconfigured opens a **"Connect Vertex AI"** dialog
    (project ID + location inputs) that writes `GOOGLE_CLOUD_PROJECT` /
    `GOOGLE_CLOUD_LOCATION` into `.env` via `update_env_values()` (preserves other `.env` lines).
- **Data Sources & Filters** (3 cards): edit `websites`, `keywords` (whitelist), and `blacklist`
  tables in `tenders.db` via modal dialogs (`websites_canvas`, `keywords_canvas`,
  `blacklist_canvas`). Each shows a "N saved" badge. Saving calls `overwrite_items()` which
  `DELETE`s and re-`INSERT`s all rows, then calls `export_all_json()`. The `blacklist` table is
  read directly by `clean.py` (`get_ui_blacklist()`) on the next run.
- **Email Delivery** (1 card): shows a connection badge —
  - Green **"Sending from `<sender>` to N recipient(s)"** (with the recipient list below) if
    `EMAIL_SENDER`, `EMAIL_PASSWORD`, and `EMAIL_RECIPIENT` are all set in `.env`.
  - Amber **"Email not configured"** otherwise.
  - "Edit email settings" opens an **"Email Notifications"** dialog (`email_setup_canvas`):
    sender Gmail address, Gmail app password, and a recipients textarea (**one email per line**
    for multiple recipients). Saving joins recipients with `", "` and writes `EMAIL_SENDER`,
    `EMAIL_PASSWORD`, `EMAIL_RECIPIENT` into `.env` via `update_env_values()`.
- **AI Strategy Prompt**: edits the single row in the `prompts` table (used by `ai.py`'s
  `build_dynamic_prompt()`).
- **Import External Data**: uploads `.xlsx`/`.xls` files (e.g. TenderTiger exports) and saves them
  to the project root — just a file save, no parsing/ingestion happens here.

`init_db()` creates these `tenders.db` tables if missing: `websites`, `keywords`, `blacklist`,
`prompts`, `past_tenders`, `past_canceled_tenders`. (`daily_good_tenders` and `inbox_tenders` exist
in `tenders.db` from earlier versions of the app but are no longer created/used.)

`export_all_json()` writes backups: `websites.json`, `keywords.json`, `blacklist.json`,
`prompts.json`, `past_tenders.json`, `past_canceled_tenders.json`.

---

## 3. Databases & Schemas

### `tenders.db` — Configuration database (hand-edited via `app.py`)
| Table | Columns | Used by |
|---|---|---|
| `websites` | `id, item` (URL) | `scraper.py` (source list) |
| `keywords` | `id, item` (regex/string) | `clean.py` (whitelist), `ai.py` (prompt keywords) |
| `blacklist` | `id, item` (regex/string) | `clean.py` (blacklist filter), editable in `app.py` |
| `prompts` | `id, item` | `ai.py` (system prompt, first row only) |
| `past_tenders` | `id, item` | `ai.py` (few-shot "winning tender" examples) |
| `past_canceled_tenders` | `id, item` | written by `export_all_json()`, otherwise unused |
| `daily_good_tenders` | `id, date, tender_id, title, url` | legacy, unused |
| `inbox_tenders` | `id, tender_id, title, summary, url` | legacy, unused |

### `raw_database.db` — Raw scrape output (rebuilt every run by `scraper.py`)
| Table | Columns |
|---|---|
| `raw_tables` | `Source_URL, Page, Row_Text, Embedded_Links` |
| `raw_pdfs` | `Source_URL, Page, Link_Text, Document_URL` |
| `raw_keywords` | `Source_URL, Page, Link_Text, Target_URL` |
| `raw_iframes` | `Source_URL, Page, IFrame_Source_URL` |
| `raw_portals` | `Source_URL, Page, Portal_Text, Portal_Link` |

### `upjao_cleansed_database.db` — Cumulative cleaned history (append-only, never wiped)
Same 5 tables/columns as `raw_database.db`, but containing **every filtered lead ever seen**
across all runs (deduped, ignoring the `Page` column). This is the "have we already emailed this?"
reference used by `clean.py`.

> This file also currently contains legacy tables (`tenders_tables`, `tenders_pdfs`,
> `tenders_keywords`, `tenders_iframes`, `tenders_portals`) from an older schema — these are not
> written to or read by the current `clean.py`.

---

## 4. Output Files

| File | Produced by | Content |
|---|---|---|
| `UPJAO_MASTER_LEADS.csv` | `clean.py` | Today's **new-only** filtered tender table rows (`Source_URL, Page, Row_Text, Embedded_Links`) |
| `UPJAO_AI_SALES_STRATEGY.csv` | `ai.py` | AI-scored version of the above: `Tender Name, Description, Size Value, Size Text, Deadline, PDF Link, Source Type, AI Match Reason, Upjao Strategy Mapping` |
| `websites.json`, `keywords.json`, `blacklist.json`, `prompts.json`, `past_tenders.json`, `past_canceled_tenders.json` | `app.py` (`export_all_json`) | JSON mirrors of the corresponding `tenders.db` tables |

---

## 5. Configuration Files

### `.env`
```
EMAIL_SENDER = "..."        # Gmail address used to send reports
EMAIL_PASSWORD = "..."      # Gmail APP PASSWORD (16-char), not the account password
EMAIL_RECIPIENT = "..."     # Comma-separated list for multiple recipients, e.g. "a@x.com, b@y.com"

GOOGLE_CLOUD_PROJECT="..."  # GCP project for Vertex AI / Gemini (ai.py). Placeholder value
                            # "add your project id here" means "not configured" — pipeline.py
                            # and app.py both check for this exact string to skip/prompt for AI.
GOOGLE_CLOUD_LOCATION="..." # Vertex AI region, e.g. "us-central1"
```
Both `ai.py` (via `load_dotenv`) and `pipeline.py` read this. `send_email.py` parses it manually
(simple line-by-line `KEY=value` parsing, ignores quotes/whitespace).

### `.streamlit/config.toml`
Theme for `app.py` — Flat/Enterprise SaaS palette (navy `#0F172A`, accent blue `#0369A1`, light
backgrounds), `sans serif` base font (Plus Jakarta Sans loaded via CSS injection in `app.py`).

### `requirements.txt`
```
playwright==1.60.0
beautifulsoup4==4.14.3
pandas==3.0.3
python-dotenv==1.2.2
pydantic==2.13.4
google-genai==2.8.0
streamlit==1.37.1
```
Standard-library modules used throughout (no install needed): `sqlite3, csv, smtplib, email, json,
os, sys, re, subprocess, threading, concurrent.futures, urllib.parse, datetime, hashlib, io`.

After `pip install -r requirements.txt`, also run:
```bash
playwright install chromium
playwright install-deps   # Linux only — installs OS libs Chromium needs
```

---

## 6. Running It

**One-time setup:**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
gcloud auth application-default login   # only if using ai.py
```

**Full daily pipeline:**
```bash
python pipeline.py
```

**Configuration UI:**
```bash
streamlit run app.py
```

**Individual stages** (in order, if running manually):
```bash
python scraper.py        # -> raw_database.db
python clean.py           # -> upjao_cleansed_database.db, UPJAO_MASTER_LEADS.csv
python send_email.py UPJAO_MASTER_LEADS.csv
python ai.py               # -> UPJAO_AI_SALES_STRATEGY.csv  (needs Vertex AI configured)
python send_email.py UPJAO_AI_SALES_STRATEGY.csv
```

**Scheduling:** intended to run once daily (e.g. 7 AM via cron on a persistent host — see
deployment notes). The persistence of `tenders.db` and `upjao_cleansed_database.db` between runs
is essential — without it, every tender looks "new" every day.

---

## 7. Project Structure

The project root contains exactly the active pipeline: `pipeline.py`, `scraper.py`, `clean.py`,
`ai.py`, `send_email.py`, `app.py`, `tenders.db`, `upjao_cleansed_database.db`, `.env`,
`requirements.txt`, and `.streamlit/config.toml`. No legacy/experimental files remain.

## 8. Git / GitHub Setup

- **`.env`** is gitignored — it contains real credentials (Gmail app password, GCP project).
  **`.env.example`** is a committed template; copy it to `.env` and fill in real values after
  cloning.
- **`.gitignore`** also excludes:
  - `raw_database.db` — ~290MB, exceeds GitHub's 100MB file limit, and is fully rebuilt by
    `scraper.py` on every run anyway.
  - `UPJAO_MASTER_LEADS.csv` / `UPJAO_AI_SALES_STRATEGY.csv` — regenerated every pipeline run.
  - `websites.json`, `keywords.json`, `blacklist.json`, `prompts.json`, `past_tenders.json`,
    `past_canceled_tenders.json` — auto-exported mirrors of `tenders.db`, regenerated by `app.py`.
  - `.venv/`, `__pycache__/`.
- **`tenders.db`** (config: websites/keywords/blacklist/prompts) and
  **`upjao_cleansed_database.db`** (cumulative lead history, ~9MB) **are committed** — this
  preserves the scraper configuration and dedup history for a fresh clone.

**First-time setup after cloning:**
```bash
cp .env.example .env   # then edit .env with real email/GCP credentials
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```
