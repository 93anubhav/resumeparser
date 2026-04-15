import io
import requests
import streamlit as st
import pdfplumber
import docx  
import re
import boto3
import uuid
import concurrent.futures 
from datetime import datetime, timedelta
from botocore.client import Config 
from boto3.dynamodb.conditions import Attr

# ================= CONFIG =================
st.set_page_config(page_title="AI Resume Matcher", page_icon="⚡", layout="wide")

# ================= UI / CSS =================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

html, body {
    font-family: 'Inter', sans-serif;
    background-color: #f6f8fb;
}

/* Header */
.header {
    display:flex;
    justify-content:space-between;
    align-items:center;
    padding:15px 20px;
    border-radius:12px;
    background: linear-gradient(90deg,#1e3a8a,#2563eb);
    color:white;
    margin-bottom:20px;
}

/* Selected Box Styling */
.selected-box {
    border-left:5px solid #16a34a;
    background:#f0fdf4;
    padding:15px;
    border-radius:8px;
    margin-bottom: 10px;
}

/* Rejected Box Styling */
.rejected-box {
    border-left:5px solid #dc2626;
    background:#fef2f2;
    padding:15px;
    border-radius:8px;
    margin-bottom: 10px;
}

.duplicate-warning {
    background-color: #fffbeb;
    border: 1px solid #f59e0b;
    padding: 20px;
    border-radius: 10px;
    color: #92400e;
    margin-bottom: 20px;
}

/* Buttons */
.stButton>button {
    background:#2563eb;
    color:white;
    border-radius:8px;
    font-weight:600;
}
</style>
""", unsafe_allow_html=True)

# ================= AWS SETUP =================
api_url = st.secrets.get("API_URL", "YOUR_API")
S3_BUCKET = "resumeparser"

@st.cache_resource
def get_aws():
    session = boto3.Session(
        region_name=st.secrets.get("AWS_REGION", "us-east-1"),
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"]
    )
    db = session.resource('dynamodb')
    # Use ap-south-1 for S3 as specified, forcing v4 signature for compatibility
    s3 = session.client('s3', region_name="ap-south-1", config=Config(signature_version='s3v4'))
    return db.Table("JobRoleDescriptionMapping"), db.Table("Resume_Metadata"), s3

jd_table, metadata_table, s3_client = get_aws()

# ================= SESSION STATE =================
if 'workflow' not in st.session_state: st.session_state.workflow = "INPUT"
if 'results' not in st.session_state: st.session_state.results = []
if 'to_process' not in st.session_state: st.session_state.to_process = []
if 'duplicates' not in st.session_state: st.session_state.duplicates = []
if 'expand_all' not in st.session_state: st.session_state.expand_all = False
if 'history_data' not in st.session_state: st.session_state.history_data = []

# ================= LOGIC FUNCTIONS =================
def clean_text(text):
    """Normalizes whitespace and removes noise for the AI."""
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_pii(file_bytes, file_name, file_type):
    """Enhanced extraction for PDFs and DOCX."""
    text = ""
    try:
        if file_name.lower().endswith(".pdf"):
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages[:3]:
                    extracted = page.extract_text(layout=True) # layout=True is critical for multi-column resumes
                    if extracted: text += extracted + "\n"
        elif file_name.lower().endswith(".docx"):
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
        
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        email = re.search(email_pattern, text)
        
        # Robust phone pattern for international/domestic formats
        phone_pattern = r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4,5}'
        phone = re.search(phone_pattern, text)

        return {
            "name": file_name,
            "text": clean_text(text)[:4500],
            "email": email.group(0) if email else "N/A",
            "mobile": phone.group(0) if phone else "N/A",
            "bytes": file_bytes,
            "type": file_type
        }
    except Exception as e:
        return {"name": file_name, "text": "", "email": "N/A", "mobile": "N/A", "error": str(e)}

def call_ai(cand_data, role, jd):
    """Worker for parallel API calls."""
    payload = {
        "job_role": role,
        "job_description": jd,
        "resumes": [{"filename": cand_data['name'], "content": cand_data['text'], "email": cand_data['email'], "mobile": cand_data['mobile']}]
    }
    try:
        r = requests.post(api_url, json=payload, timeout=50)
        res = r.json().get("body", r.json()).get("results", [])[0]
        eval_data = res.get("evaluation", {})
        return {
            **cand_data,
            "status": res.get("status"),
            "reason": eval_data.get("reasoning", "No reason provided."),
            "matched": eval_data.get("matched_skills", []),
            "missing": eval_data.get("missing_skills", [])
        }
    except:
        return {**cand_data, "status": "ERROR", "reason": "AI Connection Failed"}

# ================= SIDEBAR =================
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/5/51/IBM_logo.svg", width=120)
    st.markdown("### Evaluation Workspace")
    mode = st.radio("Navigation", ["Evaluate Resumes", "History Audit"])

    # Load Roles
    scan = jd_table.scan(ProjectionExpression="JobRole")
    roles = ["Select Role"] + sorted([i['JobRole'] for i in scan.get('Items', [])])
    role = st.selectbox("Position", roles)

    fetched_jd = ""
    if role != "Select Role":
        item = jd_table.get_item(Key={"JobRole": role}).get("Item", {})
        fetched_jd = item.get("JobDescription", "")

    jd_input = st.text_area("Job Description", value=fetched_jd, height=200)

# ================= HEADER =================
st.markdown(f"""
<div class="header">
    <h2 style="margin:0;">⚡ AI Resume Matcher</h2>
    <span>{mode}</span>
</div>
""", unsafe_allow_html=True)

# ================= EVALUATION PAGE =================
if mode == "Evaluate Resumes":

    if st.session_state.workflow == "INPUT":
        files = st.file_uploader("Upload Resumes", accept_multiple_files=True, type=["pdf", "docx"])
        
        if st.button("Start Analysis"):
            if role == "Select Role" or not files:
                st.warning("Please select a role and upload files.")
            else:
                with st.spinner("Extracting text & checking history..."):
                    # Fast Parallel Extraction
                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                        candidates = list(ex.map(lambda f: extract_pii(f.getvalue(), f.name, f.type), files))
                    
                    # 6-Month Duplicate Check
                    six_months_ago = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
                    history = metadata_table.scan(FilterExpression=Attr('Date').gte(six_months_ago)).get('Items', [])
                    
                    st.session_state.to_process, st.session_state.duplicates = [], []
                    
                    for c in candidates:
                        is_dup = next((h for h in history if (c['email'] != "N/A" and c['email'] == h.get('Email ID')) or (c['mobile'] != "N/A" and c['mobile'] == h.get('Mobile Number'))), None)
                        if is_dup:
                            c['prev_date'], c['prev_status'] = is_dup.get('Date'), is_dup.get('Status')
                            st.session_state.duplicates.append(c)
                        else:
                            st.session_state.to_process.append(c)
                    
                    st.session_state.workflow = "DUPLICATE_CHECK" if st.session_state.duplicates else "PROCESSING"
                st.rerun()

    elif st.session_state.workflow == "DUPLICATE_CHECK":
        st.markdown(f"""<div class="duplicate-warning"><h3>⚠️ {len(st.session_state.duplicates)} Duplicates Detected</h3>
        Candidates found in the 6-month history.</div>""", unsafe_allow_html=True)
        st.table([{"Name": d['name'], "Email": d['email'], "Last Applied": d['prev_date'], "Status": d['prev_status']} for d in st.session_state.duplicates])
        
        c1, c2, c3 = st.columns(3)
        if c1.button("Skip Duplicates"): st.session_state.workflow = "PROCESSING"; st.rerun()
        if c2.button("Process Everyone"): st.session_state.to_process += st.session_state.duplicates; st.session_state.workflow = "PROCESSING"; st.rerun()
        if c3.button("Cancel"): st.session_state.workflow = "INPUT"; st.rerun()

    elif st.session_state.workflow == "PROCESSING":
        st.info(f"Processing {len(st.session_state.to_process)} resumes in parallel...")
        bar = st.progress(0)
        final_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(call_ai, c, role, jd_input) for c in st.session_state.to_process]
            for i, f in enumerate(concurrent.futures.as_completed(futures)):
                final_results.append(f.result())
                bar.progress((i + 1) / len(futures))
        st.session_state.results, st.session_state.workflow = final_results, "DONE"
        st.rerun()

    elif st.session_state.workflow == "DONE":
        col_t, col_b = st.columns([8, 2])
        with col_b:
            if st.button("Toggle All Details", key="expand_eval"):
                st.session_state.expand_all = not st.session_state.expand_all
                st.rerun()
        
        for idx, c in enumerate(st.session_state.results):
            box = "selected-box" if c['status'] == "SELECTED" else "rejected-box"
            st.markdown(f'<div class="{box}"><b>{c["name"]}</b> — {c["status"]}</div>', unsafe_allow_html=True)
            with st.expander("AI Evaluation Details", expanded=st.session_state.expand_all):
                st.write(f"**AI Reasoning:** {c.get('reason')}")
                st.write(f"📞 {c['mobile']} | ✉️ {c['email']}")
                st.write(f"✅ **Matches:** {', '.join(c.get('matched', []))}")
                st.write(f"❌ **Gaps:** {', '.join(c.get('missing', []))}")
                st.download_button("Download Resume", c['bytes'], c['name'], key=f"eval_dl_{idx}")

        if st.button("New Batch"): st.session_state.workflow, st.session_state.results = "INPUT", []; st.rerun()

# ================= HISTORY PAGE =================
else:
    st.subheader("📊 Evaluation History")

    with st.form("history_filter"):
        c1, c2, c3 = st.columns(3)
        t_f = c1.selectbox("Timeframe", ["Last 7 Days", "Today", "All Time", "Custom Range"])
        s_f = c2.selectbox("Filter Status", ["All", "SELECTED", "REJECTED"])
        d_r = c3.date_input("Select Dates", [])
        if st.form_submit_button("Fetch Records"):
            data = metadata_table.scan().get('Items', [])
            now, filtered = datetime.utcnow(), []
            for i in data:
                try:
                    dt = datetime.strptime(i.get("Date", "").split(" ")[0], "%Y-%m-%d")
                    if s_f != "All" and i.get("Status") != s_f: continue
                    if t_f == "Today" and dt.date() != now.date(): continue
                    if t_f == "Last 7 Days" and dt < now - timedelta(days=7): continue
                    if t_f == "Custom Range" and len(d_r) == 2:
                        if not (d_r[0] <= dt.date() <= d_r[1]): continue
                    filtered.append(i)
                except: continue
            st.session_state.history_data = sorted(filtered, key=lambda x: x.get('Date', ''), reverse=True)

    if st.session_state.history_data:
        col_info, col_toggle = st.columns([8, 2])
        col_info.caption(f"Found {len(st.session_state.history_data)} records")
        with col_toggle:
            if st.button("Toggle All History", key="expand_hist"):
                st.session_state.expand_all = not st.session_state.expand_all
                st.rerun()

        for idx, i in enumerate(st.session_state.history_data):
            label = "✅" if i.get("Status") == "SELECTED" else "❌"
            with st.expander(f"{label} {i.get('Date')} - {i.get('Email ID')}", expanded=st.session_state.expand_all):
                st.write(f"**Mobile:** {i.get('Mobile Number')}")
                st.write(f"**Matched:** {i.get('Skills Matched')}")
                st.write(f"**Gaps:** {i.get('Skills Unmatched')}")
                if i.get('Filename'):
                    folder = "selected" if i.get("Status") == "SELECTED" else "rejected"
                    try:
                        url = s3_client.generate_presigned_url('get_object',
                            Params={'Bucket': S3_BUCKET, 'Key': f"{folder}/{i.get('Filename')}", 'ResponseContentDisposition': f"attachment; filename={i.get('Filename')}"},
                            ExpiresIn=3600)
                        st.markdown(f'<a href="{url}" target="_blank" style="text-decoration:none; background:#2563eb; color:white; padding:8px 15px; border-radius:5px; font-size:14px; font-weight:600;">💾 Download Resume</a>', unsafe_allow_html=True)
                    except: st.warning("Download link unavailable.")