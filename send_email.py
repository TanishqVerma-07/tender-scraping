import csv
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os
import sys

# ==========================================
# 1. BULLETPROOF CONFIG LOADER
# ==========================================
# This completely ignores spaces, quotes, and naming mismatches.
SENDER_EMAIL = ""
SENDER_PASSWORD = ""
RECIPIENT_EMAIL = ""

env_path = '.env'

if not os.path.exists(env_path):
    print("❌ Error: I cannot find the '.env' file. Please make sure it is in the exact same folder as this script.")
    exit()

# Force-read the file manually
with open(env_path, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            # Strip out any spaces or quote marks you might have accidentally left in
            value = value.strip().strip('"').strip("'") 
            
            if key in ["EMAIL_USER", "EMAIL_SENDER"]:
                SENDER_EMAIL = value
            elif key == "EMAIL_PASSWORD":
                SENDER_PASSWORD = value
            elif key == "EMAIL_RECIPIENT":
                RECIPIENT_EMAIL = value

if not SENDER_EMAIL or not SENDER_PASSWORD:
    print(f"❌ Error: Found the .env file, but couldn't read the credentials. Please ensure it has EMAIL_USER and EMAIL_PASSWORD.")
    exit()

# Support multiple recipients: "a@x.com, b@y.com" (comma-separated)
RECIPIENT_LIST = [r.strip() for r in RECIPIENT_EMAIL.split(',') if r.strip()]

if not RECIPIENT_LIST:
    print("❌ Error: EMAIL_RECIPIENT is empty. Add at least one recipient to .env.")
    exit()

# ==========================================
# 2. READ CSV AND BUILD HTML TABLE
# ==========================================
CSV_FILE = sys.argv[1] if len(sys.argv) > 1 else 'UPJAO_MASTER_LEADS.csv'
REPORT_TITLE = "AI Sales Strategy Report" if CSV_FILE == 'UPJAO_AI_SALES_STRATEGY.csv' else "Validated Sales Leads Report"

if not os.path.exists(CSV_FILE):
    print(f"❌ Error: Could not find '{CSV_FILE}' in this folder.")
    exit()

print(f"📧 Logging in as {SENDER_EMAIL}...")
print(f"📧 Reading data from {CSV_FILE}...")

html_content = """
<html>
<head>
<style>
    body { font-family: Arial, sans-serif; color: #333; }
    h2 { color: #2E86C1; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; font-size: 14px; }
    th { background-color: #2E86C1; color: white; padding: 12px; text-align: left; border: 1px solid #ddd; }
    td { padding: 10px; border: 1px solid #ddd; word-wrap: break-word; }
    tr:nth-child(even) { background-color: #f9f9f9; }
    tr:hover { background-color: #f1f1f1; }
    a { color: #E74C3C; font-weight: bold; text-decoration: none; }
</style>
</head>
<body>
    <h2>🏆 """ + REPORT_TITLE + """</h2>
    <table>
"""

with open(CSV_FILE, 'r', encoding='utf-8') as f:
    reader = csv.reader(f)
    try:
        headers = next(reader)
        html_content += "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>\n"
        
        row_count = 0
        for row in reader:
            if not row: continue
            row_count += 1
            html_content += "<tr>"
            for col in row:
                if "http" in col:
                    first_link = col.split(',')[0].strip()
                    html_content += f"<td><a href='{first_link}' target='_blank'>View Document 📄</a></td>"
                else:
                    html_content += f"<td>{col}</td>"
            html_content += "</tr>\n"
    except StopIteration:
        row_count = 0

html_content += "</table></body></html>"

if row_count == 0:
    print("⚠️ The CSV is empty! No email sent.")
    exit()

# ==========================================
# 3. SEND THE EMAIL
# ==========================================
print(f"📨 Sending {row_count} rows to {len(RECIPIENT_LIST)} recipient(s): {', '.join(RECIPIENT_LIST)}...")

try:
    msg = MIMEMultipart()
    msg['From'] = SENDER_EMAIL
    msg['To'] = ", ".join(RECIPIENT_LIST)
    msg['Subject'] = f"🚀 {REPORT_TITLE}: {row_count} Rows Found!"

    msg.attach(MIMEText(html_content, 'html'))

    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(SENDER_EMAIL, SENDER_PASSWORD)
    server.send_message(msg)
    server.quit()

    print("✅ Email sent successfully! Check your inbox.")

except smtplib.SMTPAuthenticationError:
    print("❌ Authentication failed. Make sure your 16-letter app password is correct.")
except Exception as e:
    print(f"❌ Failed to send email: {e}")