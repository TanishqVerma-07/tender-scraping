import os
import json
import csv
import threading
import time
import pandas as pd
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# --- AI IMPORTS ---
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional
from google import genai
from google.genai import types

# Load environment variables (Make sure your .env has GOOGLE_CLOUD_PROJECT)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ==========================================
# 1. AI PYDANTIC SCHEMAS (With Strategy Fields)
# ==========================================
class Tender(BaseModel):
    name: str
    description: str
    size_value: int
    size_text: str
    deadline_date: str
    pdf_link: Optional[str] = None
    data_source_type: Optional[str] = None 
    match_reason: str  
    actionable_strategy: str  # Gemini tells the sales team exactly what to pitch!

class TenderList(BaseModel):
    tenders: List[Tender]

# ==========================================
# 2. CONFIGURATION & GLOBALS
# ==========================================
INPUT_CSV_FILE = 'UPJAO_MASTER_LEADS.csv' 
FINAL_AI_LEADS_FILE = 'UPJAO_AI_SALES_STRATEGY.csv'

AI_WORKERS = 3  
CHUNK_SIZE = 20  

# Lock for thread-safe file saving
ai_csv_lock = threading.Lock()
total_found = 0

# ==========================================
# 3. DYNAMIC PROMPT BUILDER (Connects to Streamlit DB)
# ==========================================
def build_dynamic_prompt():
    current_date = datetime.now().strftime("%B %d, %Y")
    current_year = datetime.now().strftime("%Y")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(script_dir, 'tenders.db')
    
    # ---------------------------------------------------------
    # FALLBACK PROMPT (Just in case the DB is empty)
    # ---------------------------------------------------------
    base_prompt = "You are the Lead Procurement Director and VP of Sales for 'Upjao Agrotech Private Limited'.\nUpjao eliminates human subjectivity in grain testing using Computer Vision. We ONLY do PHYSICAL grain assessment and digital weighing. We sell: 1. AI Grain Analyzers (Upjao Easy/Ultra). 2. Moisture Meters. 3. Digital Weighing Scales.\nRED FLAGS: Discard TPH heavy machinery, JCBs, Hospitals, Chemistry Labs, and Warehousing Rentals.\nExtract valid tenders and provide a 'match_reason' and 'actionable_strategy'."

    keywords_text = ""
    examples_text = ""

    try:
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            
            # 1. GET THE MASTER PROMPT
            c.execute("SELECT item FROM prompts LIMIT 1")
            prompt_result = c.fetchone()
            if prompt_result and prompt_result[0].strip():
                base_prompt = prompt_result[0]
            
            # 2. GET THE KEYWORDS
            c.execute("SELECT item FROM keywords")
            keywords_list = [row[0].strip() for row in c.fetchall()]
            keywords_text = ", ".join(keywords_list)
            
            # 3. GET THE PAST VERIFIED TENDERS (Examples)
            c.execute("SELECT item FROM past_tenders")
            past_tenders_list = [row[0].strip() for row in c.fetchall()]
            for i, tender in enumerate(past_tenders_list):
                examples_text += f"  {i+1}. {tender}\n"
                
            conn.close()
    except Exception as e:
        print(f"  ⚠️ Could not read Streamlit DB: {e}. Using fallback prompt.")

    # 4. STITCH IT ALL TOGETHER
    dynamic_prompt = f"{base_prompt}\n\n"
    dynamic_prompt += f"TODAY's DATE: {current_date}. Only accept tenders closing in {current_year} or later, or 'Not specified'.\n\n"
    
    if keywords_text:
        dynamic_prompt += f"### OUR TARGET KEYWORDS ###\nLook specifically for tenders containing these concepts:\n{keywords_text}\n\n"
        
    if examples_text:
        dynamic_prompt += f"### EXAMPLES OF PAST WINNING TENDERS ###\nUse these exact past tenders as your benchmark for what constitutes a PERFECT match for our company:\n{examples_text}\n"

    return dynamic_prompt

# ==========================================
# 4. GEMINI 2.5 PRO EXTRACTION ENGINE
# ==========================================
def extract_tenders_via_gemini(text: str) -> List[Tender]:
    try:
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION")

        if not project_id or not location:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must be set in .env")
        client = genai.Client(vertexai=True, project=project_id, location=location)

        # Build the dynamic prompt from the database!
        system_prompt = build_dynamic_prompt()
        full_prompt = system_prompt + "\n\n--- INCOMING TENDER DATA TO EVALUATE ---\n" + text

        # USING GEMINI 2.5 PRO
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=full_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TenderList,
                temperature=0.0, 
            ),
        )

        data = json.loads(response.text) if isinstance(response.text, str) else response.text
        tender_list = TenderList(**data)

        for t in tender_list.tenders:
            t.data_source_type = "CSV_DATA"
            
        return tender_list.tenders

    except Exception as e:
        print("  ⚠️ Gemini API Error:", e)
        return []

# ==========================================
# 5. PARALLEL AI PROCESSOR FUNCTION
# ==========================================
def process_single_chunk(chunk, chunk_id):
    global total_found
    print(f"  🧠 Gemini 2.5 Pro processing Chunk {chunk_id}...")
    
    data_string = "\n".join(chunk)
    extracted = extract_tenders_via_gemini(text=data_string)
    
    if extracted:
        with ai_csv_lock:
            total_found += len(extracted)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            final_path = os.path.join(script_dir, FINAL_AI_LEADS_FILE)
            
            with open(final_path, 'a', newline='', encoding='utf-8') as f_append:
                writer = csv.writer(f_append)
                for t in extracted:
                    writer.writerow([
                        t.name, t.description, t.size_value, 
                        t.size_text, t.deadline_date, t.pdf_link, t.data_source_type, 
                        t.match_reason, t.actionable_strategy
                    ])
        print(f"    🌟 Validated {len(extracted)} Upjao Leads from Chunk {chunk_id}!")
    else:
        print(f"    - No matching Upjao leads found in Chunk {chunk_id}.")

def run_ai_in_parallel(data_list):
    chunks = [data_list[i:i + CHUNK_SIZE] for i in range(0, len(data_list), CHUNK_SIZE)]
    print(f"📦 Split CSV data into {len(chunks)} chunks of {CHUNK_SIZE} rows each.")
    
    with ThreadPoolExecutor(max_workers=AI_WORKERS) as ai_executor:
        for index, chunk in enumerate(chunks):
            ai_executor.submit(process_single_chunk, chunk, index + 1)
            time.sleep(1) # Prevent API rate limits

# ==========================================
# 6. UNIFIED MAIN EXECUTION
# ==========================================
if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_path = os.path.join(script_dir, INPUT_CSV_FILE)
    output_path = os.path.join(script_dir, FINAL_AI_LEADS_FILE)

    if not os.path.exists(input_path):
        print(f"❌ CRITICAL ERROR: '{INPUT_CSV_FILE}' not found in this folder.")
        exit()

    # 1. INITIALIZE FINAL OUTPUT FILE
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(['Tender Name', 'Description', 'Size Value', 'Size Text', 'Deadline', 'PDF Link', 'Source Type', 'AI Match Reason', 'Upjao Strategy Mapping'])
    print(f"📁 Initialized Final Output File: {FINAL_AI_LEADS_FILE}")

    # 2. READ CSV DATA
    print(f"\n📊 Reading pre-filtered data from {INPUT_CSV_FILE}...")
    try:
        df = pd.read_csv(input_path).fillna("")
        
        columns = df.columns
        payload_rows = []
        for index, row in df.iterrows():
            row_text = " | ".join([str(col) + ": " + str(row[col]) for col in columns if str(row[col]).strip() != ""])
            payload_rows.append(row_text)
            
        print(f"✅ Successfully loaded {len(payload_rows)} rows from CSV.")
    except Exception as e:
        print(f"❌ Error reading CSV file: {e}")
        exit()

    # 3. RUN AI IN PARALLEL
    print(f"\n🤖 LAUNCHING GEMINI 2.5 PRO FOR UPJAO AGROTECH ON {datetime.now().strftime('%d-%b-%Y')}...")
    if payload_rows:
        run_ai_in_parallel(payload_rows)

    print("\n=============================================")
    print("🎯 BOOM! AI OPERATION COMPLETE.")
    print(f"🏆 Gemini safely mapped {total_found} PERFECT Upjao leads into '{FINAL_AI_LEADS_FILE}'")
    print("=============================================")