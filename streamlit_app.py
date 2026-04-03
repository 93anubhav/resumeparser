import io
import requests
import streamlit as st
import pdfplumber
import json
import re

st.set_page_config(page_title="AI Resume Matcher", layout="centered")

st.markdown(
    """
    <style>
    .page-title { text-align: center; font-size:32px; color:#2a4d8f; font-weight:700; margin-bottom:18px }
    .card {background: white; padding: 26px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.06); margin-bottom: 26px}
    .label { font-weight:600; margin-top:8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="page-title">Bulk AI Resume Matcher</div>', unsafe_allow_html=True)

api_url = st.secrets.get("API_URL", "YOUR_API_GATEWAY_URL_HERE")

with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Job Role Selection")
    role = st.selectbox("Select Job Role:", ["", "Python", "Nodejs", "Java"], format_func=lambda x: ("-- Select Job Role --" if x=="" else x))
    st.markdown('</div>', unsafe_allow_html=True)

with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Upload Resumes")
    
    with st.form("resume_upload_form", clear_on_submit=True):
        uploaded_files = st.file_uploader("Choose up to 10 resume files", type=["pdf", "txt"], accept_multiple_files=True)
        submitted = st.form_submit_button("Match Resumes")

    if submitted:
        if not role:
            st.error("Please select a Job Role first.")
        elif not uploaded_files:
            st.error("Please upload at least one resume.")
        elif len(uploaded_files) > 10:
            st.error("You can only process a maximum of 10 resumes at a time.")
        else:
            st.write(f"### Evaluating candidates for {role}...")
            
            progress_bar = st.progress(0)
            
            for index, file in enumerate(uploaded_files):
                with st.spinner(f"Reading {file.name} (File {index+1} of {len(uploaded_files)})..."):
                    try:
                        resume_text = ""
                        if file.type == "application/pdf":
                            with pdfplumber.open(file) as pdf:
                                max_pages = min(len(pdf.pages), 3)
                                for i in range(max_pages):
                                    text = pdf.pages[i].extract_text()
                                    if text: 
                                        resume_text += text + " \n"
                        else:
                            resume_text = file.read().decode("utf-8", errors="ignore")

                        resume_text = resume_text.encode("ascii", "ignore").decode("ascii")

                        # ==========================================
                        # PYTHON DATA EXTRACTION (Running silently)
                        # ==========================================
                        
                        # 1. Extract Email
                        email_match = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', resume_text)
                        ext_email = email_match.group(0) if email_match else "Not Found"

                        # 2. Extract Phone Number
                        phone_match = re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4,5}', resume_text)
                        ext_mobile = phone_match.group(0) if phone_match else "Not Found"
                        
                        # Clean the phone number locally
                        if ext_mobile != "Not Found":
                            ext_mobile = re.sub(r'\D', '', ext_mobile)
                            if len(ext_mobile) > 10 and ext_mobile.startswith("91"):
                                ext_mobile = ext_mobile[2:]
                        if not ext_mobile:
                            ext_mobile = "Not Found"

                        # 3. Extract Name
                        ext_name = "Not Found"
                        lines = [line.strip() for line in resume_text.split('\n') if len(line.strip()) > 2]
                        for line in lines:
                            if line.lower() not in ["resume", "curriculum vitae", "cv", "profile"]:
                                ext_name = line
                                break

                        # ==========================================

                        # Silently send the extracted data to Lambda
                        payload = {
                            "job_role": role,
                            "resumes": [{
                                "filename": file.name,
                                "content": resume_text[:4000],
                                "candidate_name": ext_name,
                                "email": ext_email,
                                "mobile": ext_mobile
                            }]
                        }
                        
                        resp = requests.post(api_url, json=payload, timeout=60) 
                        
                        raw_data = resp.json()
                        result_json = raw_data.get("body", raw_data)
                        if isinstance(result_json, str):
                            result_json = json.loads(result_json)

                        if resp.status_code == 200:
                            results_list = result_json.get("results", [])
                            for res in results_list:
                                status = res.get("status", "ERROR")
                                filename = res.get("filename", "Unknown")
                                evaluation_data = res.get("evaluation", {})
                                
                                # --- THE FIX: expanded=False ---
                                with st.expander(f"{filename} - {status}", expanded=False):
                                    if status == "SELECTED":
                                        st.success(f"**Status:** {status} ✅")
                                    elif status == "REJECTED":
                                        st.error(f"**Status:** {status} ❌")
                                    else:
                                        st.warning(f"**Error:** {res.get('error')}")
                                    
                                    st.markdown("---")
                                    st.markdown("**AI Decision JSON:**")
                                    st.json(evaluation_data)
                        else:
                            st.error(f"API Error for {file.name}: {resp.status_code}")
                            st.write(result_json)

                    except Exception as e:
                        st.error(f"Application Error on {file.name}: {e}")
                
                progress_bar.progress((index + 1) / len(uploaded_files))
            
            st.success("Batch Processing Complete!")
    st.markdown('</div>', unsafe_allow_html=True)