import os
import sys
import subprocess
from dotenv import load_dotenv

# ==========================================
# FULL PIPELINE: tenders.db -> raw_database.db -> upjao_cleansed_database.db + UPJAO_MASTER_LEADS.csv -> UPJAO_AI_SALES_STRATEGY.csv
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TENDERS_DB = os.path.join(SCRIPT_DIR, 'tenders.db')

load_dotenv(os.path.join(SCRIPT_DIR, '.env'))


def run_step(script_name, *args):
    script_path = os.path.join(SCRIPT_DIR, script_name)
    print(f"\n🚀 Running {script_name} ...\n")
    result = subprocess.run([sys.executable, script_path, *args], cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print(f"❌ {script_name} failed with exit code {result.returncode}. Stopping pipeline.")
        sys.exit(result.returncode)


if __name__ == '__main__':
    if not os.path.exists(TENDERS_DB):
        print(f"❌ CRITICAL ERROR: '{TENDERS_DB}' not found. Add websites/keywords via the Streamlit dashboard first.")
        sys.exit(1)

    # Step 1: Scrape websites from tenders.db -> raw_database.db
    run_step('scraper.py')

    # Step 2: Clean raw_database.db -> upjao_cleansed_database.db + UPJAO_MASTER_LEADS.csv
    run_step('clean.py')

    # Step 3: Email the cleaned leads
    run_step('send_email.py', 'UPJAO_MASTER_LEADS.csv')

    # Step 4: AI sales strategy mapping -> UPJAO_AI_SALES_STRATEGY.csv (skipped if Vertex AI isn't configured)
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if project_id.strip() == "add your project id here":
        print("\n⏭️  Skipping ai.py: GOOGLE_CLOUD_PROJECT is not configured in .env (no Vertex AI connection).")
    else:
        run_step('ai.py')

        # Step 5: Email the AI sales strategy report
        run_step('send_email.py', 'UPJAO_AI_SALES_STRATEGY.csv')

    print("\n=============================================")
    print("🎉 FULL PIPELINE COMPLETE!")
    print("📥 Input:  tenders.db")
    print("📤 Output: raw_database.db, upjao_cleansed_database.db, UPJAO_MASTER_LEADS.csv, UPJAO_AI_SALES_STRATEGY.csv")
    print("=============================================")
