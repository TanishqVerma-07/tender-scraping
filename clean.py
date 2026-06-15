import pandas as pd
import sqlite3
import os
import re

# ==========================================
# 1. PULL TERMS FROM FRONTEND UI (PLAIN WORDS -> SAFE REGEX)
# ==========================================
def _load_terms(table_name, whole_word=False):
    """Reads plain words/phrases from the given tenders.db table and converts each
    into a safe regex (special characters escaped) so the user only ever has to
    type plain text in the Streamlit UI. If whole_word=True, each term is wrapped
    in \\b...\\b so it only matches as a complete word/phrase."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_db = os.path.join(script_dir, 'tenders.db')

    if not os.path.exists(dashboard_db):
        return []

    try:
        uri = f"file:{dashboard_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        c = conn.cursor()
        c.execute(f"SELECT item FROM {table_name}")
        raw_terms = [row[0].strip().lower() for row in c.fetchall() if row[0].strip()]
        conn.close()

        if whole_word:
            return [r'\b' + re.escape(term) + r'\b' for term in raw_terms]
        return [re.escape(term) for term in raw_terms]
    except Exception:
        return []

def get_ui_keywords():
    # Pull everything you pasted into Streamlit.
    # Substring matching is intentional: stems like "grain analy" must keep
    # matching "Grain Analyzer" / "Grain Analysis" / "Grain Analytics".
    return _load_terms('keywords', whole_word=False)

# ==========================================
# 2. THE GARBAGE BLACKLIST (PULLED FROM FRONTEND UI)
# ==========================================
def get_ui_blacklist():
    # Pull everything saved in the Streamlit Garbage Blacklist box.
    # Whole-word matching: "tph"/"road"/"jcb" shouldn't match inside "depth",
    # "broadband", "subjcb-style", etc.
    return _load_terms('blacklist', whole_word=True)

# ==========================================
# 3. THE CLEANING ENGINE
# ==========================================
def clean_and_export_database(input_db_name, output_db_name, export_csv_name):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_db_path = os.path.join(script_dir, input_db_name)
    output_db_path = os.path.join(script_dir, output_db_name)
    export_csv_path = os.path.join(script_dir, export_csv_name)
    
    if not os.path.exists(input_db_path):
        print(f"❌ ERROR: Raw Database '{input_db_name}' not found.")
        return

    final_whitelist = get_ui_keywords()

    if len(final_whitelist) == 0:
        print("❌ ERROR: Your Streamlit Keywords box is empty!")
        return

    final_blacklist = get_ui_blacklist()

    print(f"🔑 Loaded {len(final_whitelist)} Keywords totally from Streamlit UI.")
    print(f"🚫 Loaded {len(final_blacklist)} Blacklist terms totally from Streamlit UI.")
    print(f"🗄️ Connecting to Raw Database: {input_db_name}")

    try:
        conn_in = sqlite3.connect(input_db_path)
        conn_out = sqlite3.connect(output_db_path)

        cursor = conn_in.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]

        whitelist_pattern = '|'.join(final_whitelist)
        blacklist_pattern = '|'.join(final_blacklist) if final_blacklist else None
        
        for table_name in tables:
            print(f"\n🔄 Processing Table: [{table_name}]")
            
            df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn_in)
            raw_count = len(df)
            
            if raw_count == 0:
                continue

            # Ignore the 'Page' column to properly kill duplicates
            cols_to_check = [col for col in df.columns if col != 'Page']
            df = df.drop_duplicates(subset=cols_to_check, keep='first')
            unique_count = len(df)
            
            combined_text = df.fillna('').astype(str).apply(lambda row: ' '.join(row.values), axis=1).str.lower()

            has_whitelist = combined_text.str.contains(whitelist_pattern, case=False, regex=True)
            if blacklist_pattern:
                has_blacklist = combined_text.str.contains(blacklist_pattern, case=False, regex=True)
            else:
                has_blacklist = pd.Series(False, index=combined_text.index)

            survivors_df = df[has_whitelist & ~has_blacklist].copy()
            garbage_count = raw_count - len(survivors_df)

            # Ignore the 'Page' column when comparing against tenders we've already seen
            dedup_cols = [col for col in survivors_df.columns if col != 'Page']

            try:
                existing_df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn_out)
            except Exception:
                existing_df = pd.DataFrame(columns=survivors_df.columns)

            if not existing_df.empty:
                merged = survivors_df.merge(existing_df[dedup_cols].drop_duplicates(), on=dedup_cols, how='left', indicator=True)
                new_df = merged[merged['_merge'] == 'left_only'].drop(columns=['_merge'])
            else:
                new_df = survivors_df

            # Append only newly-seen tenders to the cumulative cleansed database
            if not new_df.empty:
                new_df.to_sql(table_name, conn_out, if_exists='append', index=False)
            elif existing_df.empty:
                survivors_df.head(0).to_sql(table_name, conn_out, if_exists='replace', index=False)

            print(f"   🧹 Removed {raw_count - unique_count} EXACT COPIES.")
            print(f"   🗑️ Destroyed {garbage_count - (raw_count - unique_count)} garbage rows.")
            print(f"   ✅ {len(survivors_df)} TARGETED leads today, {len(new_df)} are NEW (not seen before).")

            if table_name == 'raw_tables':
                print(f"   ⚡ Generating Master CSV for AI (WITH EXCEL HINDI FIX)...")
                # 🛡️ THE FIX: 'utf-8-sig' forces Excel to display Hindi fonts perfectly!
                new_df.to_csv(export_csv_path, index=False, encoding='utf-8-sig')
                print(f"   💾 Saved {len(new_df)} NEW Master Leads to: {export_csv_name}")

    except Exception as e:
        print(f"❌ Error during database processing: {e}")
    finally:
        conn_in.close()
        conn_out.close()
        
    print("\n=============================================")
    print("🎉 PIPELINE COMPLETE!")
    print("🚀 You can now run 'python ai.py'!")
    print("=============================================")

if __name__ == "__main__":
    INPUT_DB = 'raw_database.db'      
    OUTPUT_DB = 'upjao_cleansed_database.db'    
    EXPORT_CSV = 'UPJAO_MASTER_LEADS.csv' 
    
    clean_and_export_database(INPUT_DB, OUTPUT_DB, EXPORT_CSV)