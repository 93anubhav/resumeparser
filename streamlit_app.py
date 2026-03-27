import io
import requests
import streamlit as st
import PyPDF2
import json
import re

st.set_page_config(page_title="AI Resume Matcher", layout="centered")

# Simple CSS to mimic the attached design
st.markdown(
    """
    <style>
    .page-title { text-align: center; font-size:32px; color:#2a4d8f; font-weight:700; margin-bottom:18px }
    .card {background: white; padding: 26px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.06); margin-bottom: 26px}
    .label { font-weight:600; margin-top:8px; }
    .btn { background: #2a4d8f; color: white; padding: 10px 18px; border-radius: 8px; border: none; }
    .small-caption { color: #6b7280; font-size:13px }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="page-title">AI Resume Matcher</div>', unsafe_allow_html=True)

# Job descriptions used to auto-fill
JD_TEXT = {
    "": "",
    "python": (
        "Looking for a Python Developer with 5+ years of experience in backend development.\n"
        "Must have strong knowledge of OOP, problem solving, Python core, Django/Flask/FastAPI.\n"
        "Experience with REST APIs, microservices, and databases (Postgres/MySQL/MongoDB).\n"
        "Cloud experience with AWS or Azure is a plus."
    ),
    "nodejs": (
        "Looking for a NodeJS Developer with 4+ years of backend development.\n"
        "Hands-on experience with Express, REST APIs, Microservices, JWT, WebSockets.\n"
        "Good knowledge of MongoDB/MySQL/Postgres and CI/CD pipelines.\n"
        "AWS Lambda / Docker experience is preferred."
    ),
    "java": (
        "Hiring Java Developer with 5+ years of experience building enterprise applications.\n"
        "Strong in Core Java, Spring Boot, Hibernate, REST APIs, Microservices architecture.\n"
        "Experience with MySQL/Postgres/Oracle and cloud platforms (AWS/Azure).\n"
        "Knowledge of Kafka and CI/CD is preferred."
    ),
}

# Configuration: API endpoint can be set in the sidebar
api_url = st.secrets["API_URL"]

# Job role card
with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Job Role Selection")
    role = st.selectbox("Select Job Role:", ["", "python", "nodejs", "java"], format_func=lambda x: ("-- Select Job Role --" if x=="" else x.title()))
    st.markdown('</div>', unsafe_allow_html=True)

# Single resume card
with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Single Resume Match")
    st.markdown("<div class='label'>Upload Resume:</div>", unsafe_allow_html=True)
    uploaded_file = st.file_uploader("Choose a resume file", type=["pdf", "txt", "md", "docx"])

    st.markdown("<div class='label'>Job Description:</div>", unsafe_allow_html=True)
    default_jd = JD_TEXT.get(role, "")
    jd_text = st.text_area("", value=default_jd, height=140)

    # Action button
    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("Match Resume"):
            # Call API function
            if not uploaded_file:
                st.error("Please upload a resume file first.")
            else:
                with st.spinner("Uploading and matching..."):
                    try:
                        # Extract text
                        resume_text = ""
                        if uploaded_file.type == "application/pdf":
                            pdf_reader = PyPDF2.PdfReader(uploaded_file)
                            for page in pdf_reader.pages:
                                page_text = page.extract_text()
                                if page_text:
                                    resume_text += page_text + " \n"
                        else:
                            resume_text = uploaded_file.read().decode("utf-8", errors="ignore")

                        # --- PII REDACTION (The Fix for AI Refusals) ---
                        # Scrub Emails
                        resume_text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b', '[EMAIL]', resume_text)
                        # Scrub URLs (LinkedIn, GitHub)
                        resume_text = re.sub(r'https?://\S+|www\.\S+', '[URL]', resume_text)
                        # Scrub Phone Numbers
                        resume_text = re.sub(r'\+?\d[\d\s\-\(\)]{7,}\d', '[PHONE]', resume_text)
                        # Remove hidden control characters
                        resume_text = resume_text.encode("ascii", "ignore").decode("ascii")

                        payload = {
                            "resume_summary": resume_text,
                            "job_description": jd_text
                        }
                        
                        resp = requests.post(api_url, json=payload, timeout=60)
                        
                        # Unpack API Gateway response
                        raw_data = resp.json()
                        result_json = None
                        
                        if isinstance(raw_data, dict) and "body" in raw_data:
                            if isinstance(raw_data["body"], str):
                                try:
                                    result_json = json.loads(raw_data["body"])
                                except:
                                    result_json = {"error": "Invalid JSON string", "raw": raw_data["body"]}
                            else:
                                result_json = raw_data["body"]
                        else:
                            result_json = raw_data

                        if resp.status_code >= 200 and resp.status_code < 300:
                            # Catch agent safety refusals if they somehow still happen
                            if isinstance(result_json, dict) and "error" in result_json:
                                st.error("AI Guardrail Triggered")
                                st.write("The AI refused to process this document. Details below:")
                                st.json(result_json)
                            else:
                                st.subheader("Match Result")
                                
                                # --- GREEN / RED COLOR CODING LOGIC ---
                                decision = result_json.get("decision", "").upper()
                                
                                if decision == "SELECTED":
                                    # Displays a green banner with normal text size
                                    st.success(f"**Status:** {decision} ✅")
                                else:
                                    # Displays a red banner with normal text size
                                    st.error(f"**Status:** {decision} ❌")
                                
                                # Show the rest of the JSON details
                                st.json(result_json)
                        else:
                            st.error(f"API returned status {resp.status_code}")
                            st.write(result_json)

                    except requests.exceptions.RequestException as e:
                        st.error(f"Request failed: {e}")
                    except Exception as e:
                        st.error(f"Error: {e}")
    with col2:
        st.write(" ")

    st.markdown('</div>', unsafe_allow_html=True)

st.markdown("---")
st.caption("Note: This demo posts the uploaded resume as JSON to the configured API endpoint.")