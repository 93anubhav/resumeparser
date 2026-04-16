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

/* HIGH CONTRAST READ-ONLY TEXT AREA */
div[data-testid="stTextArea"] textarea:disabled {
    color: #0f172a !important;
    -webkit-text-fill-color: #0f172a !important;
    background-color: #f8fafc !important;
    opacity: 1 !important;
    font-weight: 500;
    border: 1px solid #cbd5e1 !important;
}

/* Result Cards */
.selected-box {
    border-left:5px solid #16a34a;
    background:#f0fdf4;
    padding:15px;
    border-radius:8px;
    margin-bottom: 10px;
    color: #166534;
}

.rejected-box {
    border-left:5px solid #dc2626;
    background:#fef2f2;
    padding:15px;
    border-radius:8px;
    margin-bottom: 10px;
    color: #991b1b;
}

.duplicate-warning {
    background-color: #fffbeb;
    border: 1px solid #f59e0b;
    padding: 20px;
    border-radius: 10px;
    color: #92400e;
    margin-bottom: 20px;
}

/* Buttons and Links */
.stButton>button {
    background:#2563eb;
    color:white;
    border-radius:8px;
    font-weight:600;
}

.download-link {
    display: inline-block;
    padding: 8px 16px;
    background-color: #2563eb;
    color: white !important;
    text-decoration: none;
    border-radius: 6px;
    font-weight: 600;
    font-size: 14px;
}
</style>
""", unsafe_allow_html=True)

# ================= AWS SETUP =================
api_url = st.secrets.get("API_URL", "YOUR_API_GATEWAY_URL")
S3_BUCKET = "resumeparser"

@st.cache_resource
def get_aws_resources():
    try:
        session = boto3.Session(
            region_name=st.secrets.get("AWS_REGION", "us-east-1"),
            aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"]
        )
        db = session.resource('dynamodb')
        s3 = boto3.client(
            's3',
            region_name="ap-south-1",
            aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
            config=Config(signature_version='s3v4')
        )
        return db.Table("JobRoleDescriptionMapping"), db.Table("Resume_Metadata"), s3
    except Exception as e:
        st.error(f"AWS Error: {e}")
        return None, None, None

jd_table, metadata_table, s3_client = get_aws_resources()

# ================= SESSION STATE =================
if 'workflow' not in st.session_state: st.session_state.workflow = "INPUT"
if 'results' not in st.session_state: st.session_state.results = []
if 'to_process' not in st.session_state: st.session_state.to_process = []
if 'duplicates' not in st.session_state: st.session_state.duplicates = []
if 'expand_all' not in st.session_state: st.session_state.expand_all = False
if 'history_data' not in st.session_state: st.session_state.history_data = None

# ================= HELPERS =================
@st.cache_data(ttl=600)
def fetch_jd_mapping():
    try:
        return jd_table.scan().get('Items', [])
    except:
        return []

def extract_resume_metadata(file_bytes, file_name, file_type):
    text = ""
    try:
        if file_name.lower().endswith(".pdf"):
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages[:3]:
                    pt = page.extract_text(layout=True)
                    if pt: text += pt + "\n"
        elif file_name.lower().endswith(".docx"):
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
        
        # Name Extractor (First 5 lines)
        lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 2]
        extracted_name = "Unknown Candidate"
        for line in lines[:5]:
            if not any(word in line.lower() for word in ["resume", "cv", "curriculum", "profile", "contact"]):
                extracted_name = line
                break

        email = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        phone = re.search(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4,5}', text)
        
        mobile = "N/A"
        if phone:
            digits = re.sub(r'\D', '', phone.group(0))
            mobile = digits[-10:] if len(digits) >= 10 else digits

        return {
            "name": file_name,
            "candidate_name": extracted_name,
            "text": re.sub(r'\s+', ' ', text).strip()[:4500],
            "email": email.group(0) if email else "N/A",
            "mobile": mobile,
            "bytes": file_bytes,
            "type": file_type,
            "success": True
        }
    except Exception as e:
        return {"name": file_name, "success": False, "error": str(e)}

def fire_ai_evaluation(cand, label, jd):
    payload = {
        "job_role": label, "job_description": jd,
        "resumes": [{
            "filename": cand['name'], 
            "candidate_name": cand['candidate_name'],
            "content": cand['text'], 
            "email": cand['email'], 
            "mobile": cand['mobile']
        }]
    }
    try:
        r = requests.post(api_url, json=payload, timeout=60)
        res = r.json().get("body", r.json()).get("results", [])[0]
        eval_body = res.get("evaluation", {})
        return {
            **cand, "status": res.get("status"),
            "reason": eval_body.get("reasoning", "Evaluation complete."),
            "matched": eval_body.get("matched_skills"),
            "missing": eval_body.get("missing_skills")
        }
    except:
        return {**cand, "status": "ERROR", "reason": "AI Service Timeout."}

# ================= SIDEBAR =================
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/5/51/IBM_logo.svg", width=120)
    st.markdown("### Evaluation Workspace")
    mode = st.radio("Navigation", ["Evaluate Resumes", "History Audit"], label_visibility="collapsed")
    st.divider()

    jd_records = fetch_jd_mapping()
    if jd_records:
        jrss_list = sorted(list(set(i.get('JRSS') for i in jd_records if i.get('JRSS'))))
        s_jrss = st.selectbox("1. Select JRSS", ["Select JRSS"] + jrss_list)
        
        if s_jrss != "Select JRSS":
            band_list = sorted(list(set(i.get('Band') for i in jd_records if i.get('JRSS') == s_jrss and i.get('Band'))))
            s_band = st.selectbox("2. Select BAND", ["Select BAND"] + band_list)
        else:
            s_band = st.selectbox("2. Select BAND", ["Select BAND"], disabled=True)
            
        if s_jrss != "Select JRSS" and s_band != "Select BAND":
            tech_list = sorted(list(set(i.get('Technology') for i in jd_records if i.get('JRSS') == s_jrss and i.get('Band') == s_band and i.get('Technology'))))
            s_tech = st.selectbox("3. Select Technology", ["Select Technology"] + tech_list)
        else:
            s_tech = st.selectbox("3. Select Technology", ["Select Technology"], disabled=True)
            
        final_jd = ""
        if s_tech != "Select Technology":
            match = next((i for i in jd_records if i.get('JRSS') == s_jrss and i.get('Band') == s_band and i.get('Technology') == s_tech), None)
            if match: final_jd = match.get('JobDescription', "No description found.")
        
        st.markdown("**Position Criteria (Read-only)**")
        st.text_area("JD_BOX", value=final_jd, height=300, disabled=True, label_visibility="collapsed")
    else:
        st.error("Database connection failed.")

# ================= HEADER =================
st.markdown(f'<div class="header"><h2 style="margin:0;">⚡ AI Resume Matcher</h2><span>{mode}</span></div>', unsafe_allow_html=True)

# ================= PAGE 1: EVALUATE =================
if mode == "Evaluate Resumes":
    if st.session_state.workflow == "INPUT":
        files = st.file_uploader("Upload Resumes (Max 50)", accept_multiple_files=True, type=["pdf", "docx"])
        if st.button("Start Analysis"):
            if s_tech == "Select Technology" or not files:
                st.warning("Please define all parameters and upload resumes.")
            else:
                with st.spinner("Processing documents..."):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                        cands = list(ex.map(lambda f: extract_resume_metadata(f.getvalue(), f.name, f.type), files))
                    
                    limit = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
                    audit = metadata_table.scan(FilterExpression=Attr('Date').gte(limit)).get('Items', [])
                    
                    st.session_state.to_process, st.session_state.duplicates = [], []
                    for c in cands:
                        if not c.get("success"): continue
                        # Duplicate logic: Email first, then Mobile
                        dup = next((h for h in audit if (c['email'] != "N/A" and c['email'] == h.get('Email ID'))), None)
                        if not dup:
                            dup = next((h for h in audit if (c['mobile'] != "N/A" and c['mobile'] == h.get('Mobile Number'))), None)
                        
                        if dup:
                            c['p_date'], c['p_status'] = dup.get('Date'), dup.get('Status')
                            st.session_state.duplicates.append(c)
                        else: st.session_state.to_process.append(c)
                    
                    st.session_state.workflow = "DUPLICATE_CHECK" if st.session_state.duplicates else "PROCESSING"
                st.rerun()

    elif st.session_state.workflow == "DUPLICATE_CHECK":
        st.markdown(f'<div class="duplicate-warning"><h3>⚠️ {len(st.session_state.duplicates)} Records Detected</h3>Detected within the last 180 days.</div>', unsafe_allow_html=True)
        st.table([{"Name": d['candidate_name'], "Email": d['email'], "Mobile": d['mobile'], "Last Applied": d['p_date'], "Result": d['p_status']} for d in st.session_state.duplicates])
        c1, c2, c3 = st.columns(3)
        if c1.button("Skip Duplicates"): st.session_state.workflow = "PROCESSING"; st.rerun()
        if c2.button("Re-process Everyone"): st.session_state.to_process += st.session_state.duplicates; st.session_state.workflow = "PROCESSING"; st.rerun()
        if c3.button("Reset Batch"): st.session_state.workflow = "INPUT"; st.rerun()

    elif st.session_state.workflow == "PROCESSING":
        st.info(f"AI Engine screening {len(st.session_state.to_process)} resumes...")
        pb = st.progress(0)
        final_list = []
        label = f"{s_jrss} ({s_tech}) - Band {s_band}"
        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as ex:
            futures = [ex.submit(fire_ai_evaluation, c, label, final_jd) for c in st.session_state.to_process]
            for i, f in enumerate(concurrent.futures.as_completed(futures)):
                final_list.append(f.result())
                pb.progress((i + 1) / len(futures))
        st.session_state.results, st.session_state.workflow = final_list, "DONE"
        st.rerun()

    elif st.session_state.workflow == "DONE":
        cl, cr = st.columns([8, 2])
        with cr:
            if st.button("Expand/Collapse All"):
                st.session_state.expand_all = not st.session_state.expand_all; st.rerun()
        
        for idx, c in enumerate(st.session_state.results):
            box = "selected-box" if c['status'] == "SELECTED" else "rejected-box"
            st.markdown(f'<div class="{box}"><b>{c["candidate_name"]}</b> — {c["status"]}</div>', unsafe_allow_html=True)
            with st.expander("Analysis Insights", expanded=st.session_state.expand_all):
                st.write(f"**AI Reasoning:** {c.get('reason')}")
                st.write(f"📞 {c['email']} | ✉️ {c['mobile']}")
                st.write(f"✅ **Matched:** {c.get('matched', 'N/A')}")
                st.write(f"❌ **Missing:** {c.get('missing', 'N/A')}")
                st.download_button("Download Resume", c['bytes'], c['name'], key=f"dl_{idx}")

        if st.button("New Batch Analysis"):
            st.session_state.workflow, st.session_state.results = "INPUT", []; st.rerun()

# ================= PAGE 2: HISTORY =================
else:
    st.subheader("📊 Audit & History Dashboard")
    
    c1, c2, c3 = st.columns(3)
    win = c1.selectbox("Time Window", ["Last 7 Days", "Today", "All Time", "Custom Range"])
    stat = c2.selectbox("Filter Result", ["All", "SELECTED", "REJECTED"])
    rng = c3.date_input("Date Selection", []) if win == "Custom Range" else []

    if st.button("Fetch Audit Records", use_container_width=True):
        with st.spinner("Synchronizing records..."):
            items = metadata_table.scan().get('Items', [])
            now, filtered = datetime.utcnow(), []
            for i in items:
                try:
                    dt = datetime.strptime(i.get("Date", "").split(" ")[0], "%Y-%m-%d")
                    if stat != "All" and i.get("Status") != stat: continue
                    if win == "Today" and dt.date() != now.date(): continue
                    if win == "Last 7 Days" and dt < now - timedelta(days=7): continue
                    if win == "Custom Range" and len(rng) == 2:
                        if not (rng[0] <= dt.date() <= rng[1]): continue
                    filtered.append(i)
                except: continue
            st.session_state.history_data = sorted(filtered, key=lambda x: x.get('Date', ''), reverse=True)

    if st.session_state.history_data is not None:
        if len(st.session_state.history_data) == 0:
            st.info("0 records fetched matching your current criteria.")
        else:
            col_info, col_toggle = st.columns([8, 2])
            col_info.metric("Records Found", len(st.session_state.history_data))
            with col_toggle:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("Expand/Collapse History"):
                    st.session_state.expand_all = not st.session_state.expand_all; st.rerun()

            for idx, i in enumerate(st.session_state.history_data):
                tag = "✅" if i.get("Status") == "SELECTED" else "❌"
                header_display = i.get('Candidate Name', i.get('Filename', 'Unknown Candidate'))
                
                with st.expander(f"{tag} {i.get('Date')} — {header_display}", expanded=st.session_state.expand_all):
                    st.write(f"**Email ID:** {i.get('Email ID')}")
                    st.write(f"**Mobile Number:** {i.get('Mobile Number')}")
                    st.write(f"✅ **Matched:** {i.get('Skills Matched', 'N/A')}")
                    st.write(f"❌ **Missing:** {i.get('Skills Unmatched', 'N/A')}")
                    if i.get('Filename'):
                        folder = "selected" if i.get("Status") == "SELECTED" else "rejected"
                        try:
                            url = s3_client.generate_presigned_url('get_object', Params={'Bucket': S3_BUCKET, 'Key': f"{folder}/{i.get('Filename')}", 'ResponseContentDisposition': f"attachment; filename={i.get('Filename')}"}, ExpiresIn=3600)
                            st.markdown(f'<a href="{url}" target="_blank" class="download-link">💾 Download from S3</a>', unsafe_allow_html=True)
                        except: st.warning("S3 access error.")