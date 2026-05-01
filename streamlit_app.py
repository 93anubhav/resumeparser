import io
import requests
import streamlit as st
import pdfplumber
import docx
import re
import boto3
import uuid
import json
import concurrent.futures 
from datetime import datetime, timedelta
from botocore.client import Config 
from boto3.dynamodb.conditions import Attr

# ================= CONFIG =================
st.set_page_config(page_title="AI Resume Matcher", page_icon="⚡", layout="wide")

# CLEAN UI CSS: Hides Header, Footer, Menu, Deploy Button, and Status/Profile widgets
st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    header {visibility: hidden;}
    footer {visibility: hidden;}
    .stAppDeployButton {display: none;}
    [data-testid="stStatusWidget"] {display: none;}
    .viewerBadge_container__1QSob {display: none !important;}
    .styles_viewerBadge__1yB_j {display: none !important;}
</style>
""", unsafe_allow_html=True)

# ================= UI / CSS =================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
html, body { font-family: 'Inter', sans-serif; background-color: #f6f8fb; }
.header { display:flex; justify-content:space-between; align-items:center; padding:15px 20px; border-radius:12px; background: linear-gradient(90deg,#1e3a8a,#2563eb); color:white; margin-bottom:20px; }
div[data-testid="stTextArea"] textarea:disabled { color: #0f172a !important; -webkit-text-fill-color: #0f172a !important; background-color: #f8fafc !important; opacity: 1 !important; font-weight: 500; border: 1px solid #cbd5e1 !important; }
.selected-box { border-left:5px solid #16a34a; background:#f0fdf4; padding:15px; border-radius:8px; margin-bottom: 10px; color: #166534; }
.rejected-box { border-left:5px solid #dc2626; background:#fef2f2; padding:15px; border-radius:8px; margin-bottom: 10px; color: #991b1b; }
.duplicate-warning { background-color: #fffbeb; border: 1px solid #f59e0b; padding: 20px; border-radius: 10px; color: #92400e; margin-bottom: 20px; }
.stButton>button { background:#2563eb; color:white; border-radius:8px; font-weight:600; }
.download-link { display: inline-block; padding: 8px 16px; background-color: #2563eb; color: white !important; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px; }
</style>
""", unsafe_allow_html=True)

# ================= AWS SETUP =================
@st.cache_resource
def get_aws_resources():
    session = boto3.Session(region_name=st.secrets.get("AWS_REGION", "us-east-1"), aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"], aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"])
    db = session.resource('dynamodb')
    s3 = boto3.client('s3', region_name="ap-south-1", config=Config(signature_version='s3v4'))
    return db.Table("JobRoleDescriptionMapping"), db.Table("Resume_Metadata"), s3

api_url = st.secrets.get("API_URL")
S3_BUCKET = "resumeparser"
jd_table, metadata_table, s3_client = get_aws_resources()

# Session State
for key, val in [('workflow','INPUT'),('results',[]),('to_process',[]),('duplicates',[]),('expand_all',False),('history_data',None),('uploader_key','0'),('limit_error',False)]:
    st.session_state.setdefault(key, val)

# ================= HELPERS =================
def extract_resume_metadata(file_bytes, file_name, file_type):
    text = ""
    try:
        if file_name.lower().endswith(".pdf"):
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages[:3]:
                    pt = page.extract_text(layout=True); text += (pt + "\n") if pt else ""
        elif file_name.lower().endswith(".docx"):
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
        
        s3_client.put_object(Bucket=S3_BUCKET, Key=f"raw/{file_name}", Body=file_bytes, ContentType=file_type)
        lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 2]
        extracted_name = next((l for l in lines[:5] if not any(w in l.lower() for w in ["resume","cv","profile"])), "Unknown Candidate")
        email = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        phone = re.search(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4,5}', text)
        digits = re.sub(r'\D', '', phone.group(0)) if phone else ""
        
        return {"name": file_name, "candidate_name": extracted_name, "text": text[:4500], "email": email.group(0) if email else "N/A", "mobile": digits[-10:] if len(digits) >= 10 else "N/A", "bytes": file_bytes, "type": file_type, "success": True}
    except Exception as e: return {"name": file_name, "success": False, "error": str(e)}

def fire_ai_evaluation(resumes_list, label, jd):
    payload = {
        "job_role": label, "job_description": jd, 
        "resumes": [{"filename": r['name'], "candidate_name": r['candidate_name'], "content": r['text'], "email": r['email'], "mobile": r['mobile']} for r in resumes_list]
    }
    try:
        r = requests.post(api_url, json=payload, timeout=120)
        if r.status_code != 200: return []
        res = r.json()
        if isinstance(res.get("body"), str): res = json.loads(res["body"])
        elif "body" in res: res = res["body"]
        
        results_map = {item['filename']: item for item in res.get("results", [])}
        processed_results = []
        for orig in resumes_list:
            eval_item = results_map.get(orig['name'], {})
            eval_b = eval_item.get("evaluation", {})
            processed_results.append({
                **orig, "status": eval_item.get("status", "REJECTED"), 
                "reason": eval_b.get("reasoning", "No evaluation data"), 
                "matched": eval_b.get("matched_skills", "N/A"), 
                "missing": eval_b.get("missing_skills", "N/A")
            })
        return processed_results
    except: return []

# ================= SIDEBAR =================
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/5/51/IBM_logo.svg", width=120)
    st.markdown("### Evaluation Workspace")
    mode = st.radio("Navigation", ["Evaluate Resumes", "History Audit"], label_visibility="collapsed")
    st.divider()
    jd_recs = jd_table.scan().get('Items', []) if jd_table else []
    if jd_recs:
        s_jrss = st.selectbox("1. Select JRSS", ["Select JRSS"] + sorted(list(set(i.get('JRSS') for i in jd_recs if i.get('JRSS')))))
        s_band = st.selectbox("2. Select BAND", ["Select BAND"] + sorted(list(set(i.get('Band') for i in jd_recs if i.get('JRSS') == s_jrss))), disabled=(s_jrss=="Select JRSS"))
        s_tech = st.selectbox("3. Select Technology", ["Select Technology"] + sorted(list(set(i.get('Technology') for i in jd_recs if i.get('JRSS') == s_jrss and i.get('Band') == s_band))), disabled=(s_band=="Select BAND"))
        final_jd = next((i.get('JobDescription','') for i in jd_recs if i.get('JRSS')==s_jrss and i.get('Band')==s_band and i.get('Technology')==s_tech), "")
        st.markdown("**Position Criteria (Read-only)**")
        st.text_area("JD_SIDE", value=final_jd, height=300, disabled=True, label_visibility="collapsed")

st.markdown(f'<div class="header"><h2>⚡ AI Resume Matcher</h2><span>{mode}</span></div>', unsafe_allow_html=True)

# ================= PAGES =================
if mode == "Evaluate Resumes":
    if st.session_state.workflow == "INPUT":
        if st.session_state.limit_error:
            st.error("Limit exceeded: Please upload a maximum of 35 resumes at a time.")
            st.session_state.limit_error = False

        files = st.file_uploader("Upload Resumes", accept_multiple_files=True, type=["pdf","docx"], key=st.session_state.uploader_key)
        do_check = st.checkbox("Perform 6-month duplicate check", value=True)
        
        if files and len(files) > 35:
            st.session_state.uploader_key = str(uuid.uuid4()); st.session_state.limit_error = True; st.rerun()

        if st.button("Start AI Analysis"):
            if not files: st.error("❌ Please upload resumes to proceed.")
            elif s_jrss == "Select JRSS": st.error("❌ Please select a JRSS.")
            elif s_band == "Select BAND": st.error("❌ Please select a Band.")
            elif s_tech == "Select Technology": st.error("❌ Please select a Technology.")
            else:
                with st.spinner("Processing documents..."):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
                        cands = list(ex.map(lambda f: extract_resume_metadata(f.getvalue(), f.name, f.type), files))
                    
                    if do_check:
                        limit_dt = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
                        audit = metadata_table.scan(FilterExpression=Attr('Date').gte(limit_dt), ProjectionExpression="#em, #mob, #dt, #stat", ExpressionAttributeNames={"#em": "Email ID", "#mob": "Mobile Number", "#dt": "Date", "#stat": "Status"}).get('Items', [])
                        st.session_state.to_process, st.session_state.duplicates = [], []
                        for c in cands:
                            dup = next((h for h in audit if (c['email'] != "N/A" and c['email'] == h.get('Email ID')) or (c['mobile'] != "N/A" and c['mobile'] == h.get('Mobile Number'))), None)
                            if dup: c['p_date'], c['p_status'] = dup.get('Date'), dup.get('Status'); st.session_state.duplicates.append(c)
                            else: st.session_state.to_process.append(c)
                        st.session_state.workflow = "DUPLICATE_CHECK" if st.session_state.duplicates else "PROCESSING"
                    else:
                        st.session_state.to_process, st.session_state.duplicates = cands, []; st.session_state.workflow = "PROCESSING"
                    st.rerun()

    elif st.session_state.workflow == "DUPLICATE_CHECK":
        st.markdown(f'<div class="duplicate-warning"><h3>⚠️ {len(st.session_state.duplicates)} Records Detected</h3>Previous applications found within the last 180 days.</div>', unsafe_allow_html=True)
        st.table([{"Name": d['candidate_name'], "Email": d['email'], "Mobile": d['mobile'], "Last Applied": d['p_date'], "Result": d['p_status']} for d in st.session_state.duplicates])
        c1, c2, c3 = st.columns(3); 
        if c1.button("Skip Duplicates"): st.session_state.workflow = "PROCESSING"; st.rerun()
        if c2.button("Re-process Everyone"): st.session_state.to_process += st.session_state.duplicates; st.session_state.workflow = "PROCESSING"; st.rerun()
        if c3.button("Reset Batch"): st.session_state.workflow = "INPUT"; st.rerun()

    elif st.session_state.workflow == "PROCESSING":
        st.info(f"Processing {len(st.session_state.to_process)} resumes in batch mode...")
        label = f"{s_jrss} ({s_tech}) - {s_band}"
        st.session_state.results = fire_ai_evaluation(st.session_state.to_process, label, final_jd)
        st.session_state.workflow = "DONE"; st.rerun()

    elif st.session_state.workflow == "DONE":
        if st.button("Expand/Collapse All"): st.session_state.expand_all = not st.session_state.get('expand_all', False); st.rerun()
        for idx, c in enumerate(st.session_state.results):
            box = "selected-box" if str(c.get("status", "")).strip().upper() == "SELECTED" else "rejected-box"
            st.markdown(f'<div class="{box}"><b>{c["candidate_name"]}</b> — {c["status"]}</div>', unsafe_allow_html=True)
            with st.expander("Analysis Insights", expanded=st.session_state.get('expand_all', False)):
                st.write(f"**Reasoning:** {c.get('reason')}"); st.write(f"📞 {c['email']} | ✉️ {c['mobile']}")
                st.write(f"✅ **Matched:** {c.get('matched', 'N/A')}"); st.write(f"❌ **Missing:** {c.get('missing', 'N/A')}")
                if "bytes" in c: st.download_button("Download Original", c['bytes'], c['name'], key=f"dl_{idx}")
        if st.button("New Batch"): st.session_state.workflow, st.session_state.results = "INPUT", []; st.rerun()

else:
    # History Page Logic
    st.subheader("📊 Screening Audit Trail")
    c1, c2, c3 = st.columns(3)
    win = c1.selectbox("Time Window", ["Last 7 Days", "Today", "All Time", "Custom Range"])
    stat = c2.selectbox("Filter Status", ["All", "SELECTED", "REJECTED"])
    rng = c3.date_input("Date Range", []) if win == "Custom Range" else []
    
    if st.button("Fetch Records", use_container_width=True):
        items = metadata_table.scan().get('Items', [])
        now, filtered = datetime.utcnow(), []
        for i in items:
            try:
                dt = datetime.strptime(i.get("Date", "").split(" ")[0], "%Y-%m-%d")
                db_stat = str(i.get("Status", "")).strip().upper()
                if stat != "All" and db_stat != stat: continue
                if win == "Today" and dt.date() != now.date(): continue
                if win == "Last 7 Days" and dt < now - timedelta(days=7): continue
                if win == "Custom Range" and len(rng) == 2 and not (rng[0] <= dt.date() <= rng[1]): continue
                filtered.append(i)
            except: continue
        st.session_state.history_data = sorted(filtered, key=lambda x: x.get('Date', ''), reverse=True)
    
    if st.session_state.get('history_data') is not None:
        if not st.session_state.history_data: st.info("0 records found.")
        else:
            if st.button("Toggle Detail View"): st.session_state.expand_all = not st.session_state.get('expand_all', False); st.rerun()
            for idx, i in enumerate(st.session_state.history_data):
                db_stat = str(i.get("Status", "")).strip().upper()
                tag = "✅" if db_stat == "SELECTED" else "❌"
                with st.expander(f"{tag} {i.get('Date')} — {i.get('Candidate Name', 'Unknown')}", expanded=st.session_state.get('expand_all', False)):
                    st.write(f"**Email ID:** {i.get('Email ID')}"); st.write(f"**Mobile Number:** {i.get('Mobile Number')}")
                    st.write(f"✅ **Matched:** {i.get('Skills Matched', 'N/A')}"); st.write(f"❌ **Missing:** {i.get('Skills Unmatched', 'N/A')}")
                    url = s3_client.generate_presigned_url('get_object', Params={'Bucket': S3_BUCKET, 'Key': f"{'selected' if db_stat=='SELECTED' else 'rejected'}/{i.get('Filename')}"}, ExpiresIn=3600)
                    st.markdown(f'<a href="{url}" target="_blank" class="download-link">💾 Download Original File</a>', unsafe_allow_html=True)