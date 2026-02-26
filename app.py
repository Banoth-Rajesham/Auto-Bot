import streamlit as st
import asyncio
import threading
import salesforce_survey_bot
import importlib 
# Force reload logic
importlib.reload(salesforce_survey_bot)
from salesforce_survey_bot import SurveyBot
import time
import sys
import os
import csv
import pandas as pd
import requests
import subprocess

# FIX: Windows specific event loop policy for Playwright
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# CLOUD COMPATIBILITY: Install Playwright Browser Binaries
# This is required for Streamlit Cloud as it doesn't have them by default.
try:
    # Only run if not on Windows (presumably Cloud) or if needed
    if sys.platform != 'win32':
        subprocess.run(["playwright", "install", "chromium"], check=True)
except:
    pass

# ==========================================
# STREAMLIT UI SETUP
# ==========================================
st.set_page_config(
    page_title="Salesforce Survey Auto-Bot",
    page_icon="🤖",
    layout="wide"
)

# Custom CSS for "Premium" look
st.markdown("""
<style>
    .reportview-container {
        background: #0e1117;
    }
    .main > div {
        padding-top: 2rem;
    }
    .stTextArea textarea {
        background-color: #1e2130;
        color: #fff;
    }
    .stButton>button {
        width: 100%;
        background-color: #0078d4;
        color: white;
        border-radius: 8px;
        height: 50px;
        font-size: 18px;
    }
    .stButton>button:hover {
        background-color: #005a9e;
    }
    .log-box {
        background-color: #000;
        color: #0f0;
        font-family: 'Courier New', monospace;
        padding: 10px;
        border-radius: 5px;
        height: 400px;
        overflow-y: auto;
        font-size: 12px;
        white-space: pre-wrap;
    }
    h1 {
        color: #00a8e8;
    }
</style>
""", unsafe_allow_html=True)

st.title("🤖 Salesforce Survey Auto-Bot")
st.markdown("Automate survey submissions with specific ticket matching.")

# ==========================================
# SIDEBAR CONFIGURATION
# ==========================================
with st.sidebar:
    st.header("⚙️ Configuration")
    
    # Mode Selection
    mode = st.radio("Operation Mode", ["Scan Range (Auto)", "Direct List (Manual)"])
    is_manual = mode == "Direct List (Manual)"

    if not is_manual:
        start_id = st.number_input("Start Survey ID", value=1000, step=1)
        run_unlimited = st.checkbox("Run Indefinitely (Unlimited Mode)", value=False)
        if not run_unlimited:
            end_id = st.number_input("End Survey ID", value=1050, step=1)
        else:
            end_id = None
    else:
        st.info("In Direct Mode, the bot will open each ID from the list below and wait for you to submit.")
        start_id = 0
        end_id = 0

    st.divider()
    
    # PROXY CONFIGURATION
    st.subheader("🛡️ Anti-Ban Proxies")
    use_auto_proxy = st.checkbox("⚡ Auto-Fetch Free Proxies (Beta)", help="Automatically find free public proxies from the internet. Warning: These can be slower than your own connection.")
    
    proxies_input = st.text_area("Enter Custom Proxies (Optional)", height=100, help="Enter one proxy per line. Format: http://user:pass@ip:port")
    proxies_list = [p.strip() for p in proxies_input.split('\n') if p.strip()]

    st.divider()

    if st.sidebar.button("🗑️ Reset History"):
        if os.path.exists("completed.csv"):
            with open("completed.csv", "w", newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "SurveyID", "TicketNumber", "Status"])
            st.session_state.logs.append("🗑️ History cleared! All IDs will be retried.")
            st.rerun()
    
    st.subheader("Performance \U0001f680")
    conc_limit = st.slider("Concurrency (per worker)", 10, 300, 100, help="Concurrent HTTP requests per proxy worker. With 5 proxies at 100 = 500 total concurrent requests.")

    st.subheader("Timing (Seconds)")
    min_delay = st.slider("Min Delay Between Batches", 0.0, 5.0, 0.2)
    max_delay = st.slider("Max Delay Between Batches", 0.0, 10.0, 0.8)
    
    speed_config = (min_delay, max_delay)
    
    st.divider()
    auto_submit = st.checkbox("⚠️ Enable Auto-Submit", value=False, help="If checked, the bot will submit the form automatically. If unchecked, it will wait for you to submit.")

    st.info("Browser will open in a separate window.")

# ==========================================
# MAIN TABS
# ==========================================
tab1, tab2 = st.tabs(["🚀 Automation", "📊 Dashboard"])

with tab1:
    col1, col2 = st.columns([1, 1])

    # --- Left Column: Inputs ---
    with col1:
        st.subheader("📋 Input Data")
        
        label = "Paste Survey IDs (one per line)" if is_manual else "Paste Ticket Numbers to Watch For"
        
        # Load default tickets if file exists
        default_tickets = ""
        try:
            with open("tickets.txt", "r") as f:
                default_tickets = f.read()
        except:
            pass

        tickets_input = st.text_area(
            label, 
            value=default_tickets,
            height=300,
            help="In Direct Mode: Paste the exact Survey IDs (e.g. I260...). In Scan Mode: Paste the Ticket Numbers."
        )
        
        start_btn = st.button("🚀 Start Automation")
        stop_btn = st.button("🛑 Stop")

    # --- Right Column: Logs ---
    with col2:
        st.subheader("📟 Live Logs")
        log_placeholder = st.empty()
        st.info("ℹ️ Browser will open in a separate window (Chrome).")


# ==========================================
# DASHBOARD TAB - Results & Download
# ==========================================
with tab2:
    st.title("\U0001f4ca Results & Analytics")

    # --- MATCHED SURVEY LINKS (main feature) ---
    st.subheader("\U0001f517 Matched Survey Links")
    results_file = "survey_results.csv"
    if os.path.exists(results_file):
        try:
            rdf = pd.read_csv(results_file)
            if not rdf.empty:
                st.success(f"\u2705 {len(rdf)} survey link(s) found! Click links below to open and give ratings.")
                st.dataframe(rdf, use_container_width=True)

                # Download as CSV
                csv_data = rdf.to_csv(index=False)
                st.download_button(
                    label="\U0001f4e5 Download Results (CSV)",
                    data=csv_data,
                    file_name="survey_results.csv",
                    mime="text/csv",
                )

                # Download as Excel
                try:
                    from io import BytesIO
                    buffer = BytesIO()
                    rdf.to_excel(buffer, index=False, engine='openpyxl')
                    st.download_button(
                        label="\U0001f4e5 Download Results (Excel)",
                        data=buffer.getvalue(),
                        file_name="survey_results.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                except:
                    pass  # openpyxl not available
            else:
                st.info("No matches found yet. Run a scan first.")
        except Exception as e:
            st.error(f"Could not load results: {e}")
    else:
        st.info("No results file yet. Run a scan to find your ticket links.")

    st.divider()

    # --- SCAN HISTORY ---
    st.subheader("\U0001f4c8 Scan History")
    if os.path.exists("completed.csv"):
        try:
            df = pd.read_csv("completed.csv")
            if not df.empty:
                m1, m2, m3 = st.columns(3)
                m1.metric("Total Scans Logged", len(df))
                m2.metric("Matches Found", len(df[df['Status'].str.contains('Match|Success', na=False)]))

                try:
                    df['dt'] = pd.to_datetime(df['Timestamp'])
                    df['hour'] = df['dt'].dt.hour
                    hourly_counts = df.groupby('hour').size()
                    st.subheader("Activity Timeline (Hourly)")
                    st.bar_chart(hourly_counts)
                except:
                    pass

                st.subheader("Recent Activity")
                st.dataframe(df.tail(20))
            else:
                st.info("No data yet.")
        except Exception as e:
            st.error(f"Could not load analytics: {e}")
    else:
        st.info("History file not found yet.")

# ==========================================
# AUTOMATION HANDLER
# ==========================================

# Use Session State to manage the logs
if "logs" not in st.session_state:
    st.session_state.logs = []

def get_free_proxies():
    """Fetch free proxies from public API."""
    try:
        url = "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            # Return list of 'http://ip:port'
            raw_proxies = response.text.split('\r\n')
            return [f"http://{p}" for p in raw_proxies if p.strip()]
    except:
        pass
    return []

# Main Logic
if start_btn:
    st.session_state.logs = [] # Clear logs
    
    # Auto-Fetch Logic
    if use_auto_proxy:
        with st.status("🔄 Fetching fresh proxies...", expanded=True) as p_status:
            fetched = get_free_proxies()
            if fetched:
                proxies_list.extend(fetched)
                # Deduplicate
                proxies_list = list(set(proxies_list))
                p_status.update(label=f"✅ Loaded {len(proxies_list)} Free Proxies!", state="complete", expanded=False)
            else:
                p_status.update(label="❌ Coupled not fetch proxies. Using direct connection.", state="error")
    
    # Save tickets to file first
    with open("tickets.txt", "w") as f:
        f.write(tickets_input)
    
    # Get ticket list (handle newlines and commas)
    raw_tickets = tickets_input.replace(",", "\n")
    ticket_list = [t.strip() for t in raw_tickets.split('\n') if t.strip()]
    
    if not ticket_list and is_manual:
        st.error("Please provide at least one Survey ID for Direct Mode.")
    elif not ticket_list and not is_manual:
        st.warning("No specific tickets provided. Running in broad scan mode.")
    
    # Proceed if valid
    if (ticket_list or not is_manual):
        # Define callback
        def log_callback(msg):
            st.session_state.logs.append(msg)
            # Force update of log box
            log_placeholder.markdown(f'<div class="log-box">{"".join([l + "<br>" for l in st.session_state.logs])}</div>', unsafe_allow_html=True)

        # Initialize with backward compat, then set attributes
        bot = SurveyBot(start_id, end_id, ticket_list, speed_delay=speed_config, manual_mode=is_manual, auto_submit=auto_submit)
        bot.batch_size = conc_limit
        bot.proxies = proxies_list
        
        with st.status("Running Automation...", expanded=True) as status:
            try:
                # Run synchronously
                bot.run(progress_callback=log_callback)
                status.update(label="Automation Complete!", state="complete", expanded=False)
            except Exception as e:
                st.error(f"Critical Error: {e}")
                status.update(label="Failed", state="error")            

if stop_btn:
    st.warning("To stop a running synchronous process, please close the browser or terminal.")
