import streamlit as st
from agent.calendar_agent import run_agent
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
import requests
import json
from typing import Dict, Any, List, Optional
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize session state
if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.context = {}
    st.session_state.llm_status = "active"
    st.session_state.user_settings = {
        "timezone": "UTC",
        "working_hours": {"start": "09:00", "end": "17:00"},
        "default_duration": 60
    }

# Set page config
st.set_page_config(
    page_title="🌟 Elite Calendar Assistant",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for enhanced UI
st.markdown("""
<style>
    .stChatMessage {
        padding: 1rem;
        border-radius: 0.5rem;
        margin-bottom: 1rem;
    }
    .assistant-message {
        background-color: #f0f2f6;
    }
    .user-message {
        background-color: #e3f2fd;
    }
    .time-slot {
        padding: 0.5rem;
        margin: 0.25rem 0;
        border-radius: 0.25rem;
        background-color: #e8f5e9;
    }
    .error-message {
        color: #d32f2f;
        background-color: #fce4ec;
        padding: 0.5rem;
        border-radius: 0.25rem;
    }
</style>
""", unsafe_allow_html=True)

def get_upcoming_events(days: int = 7) -> List[Dict[str, Any]]:
    """Enhanced event fetcher with better error handling"""
    try:
        backend_url = os.getenv('BACKEND_URL', 'http://127.0.0.1:8000')
        if not backend_url:
            st.warning("Backend URL not configured")
            return []

        # First check if backend is reachable
        try:
            ping_response = requests.get(f"{backend_url}/", timeout=2)
            if ping_response.status_code != 200:
                st.toast("⚠️ Backend service is not responding properly", icon="⚠️")
                return []
        except requests.exceptions.RequestException:
            st.toast("⚠️ Could not connect to backend service", icon="⚠️")
            return []

        now = datetime.utcnow().isoformat() + "Z"
        end_date = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
        
        response = requests.get(
            f"{backend_url}/availability",
            params={
                "start_time": now,
                "end_time": end_date,
                "detailed": True
            },
            timeout=15
        )
        
        if response.status_code == 500:
            error_detail = response.json().get("detail", "Unknown backend error")
            st.toast(f"⚠️ Backend error: {error_detail}", icon="⚠️")
            return []
            
        response.raise_for_status()
        return response.json().get("busy_slots", [])
        
    except Exception as e:
        logger.error(f"Error loading events: {str(e)}")
        return []

def display_time_slots(slots: List[Dict[str, str]]) -> None:
    """Beautiful time slot display with enhanced UI"""
    if not slots:
        st.info("No available time slots found")
        return
        
    for slot in slots:
        with st.container():
            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"""
                <div class="time-slot">
                    <b>{slot['start']} - {slot['end']}</b><br>
                    <small>Available for booking</small>
                </div>
                """, unsafe_allow_html=True)
            with col2:
                if st.button("Book", key=f"book_{slot['start']}"):
                    handle_slot_selection(slot)

def handle_slot_selection(slot: Dict[str, str]) -> None:
    """Handle time slot selection"""
    st.session_state.context["proposed_slot"] = slot
    st.session_state.messages.append({
        "role": "assistant",
        "content": f"Selected time: {slot['start']} - {slot['end']}. What's the meeting about?"
    })
    st.rerun()

def handle_reschedule_init(event: Dict[str, Any]) -> None:
    """Initialize rescheduling process"""
    st.session_state.context["rescheduling"] = event
    st.session_state.messages.append({
        "role": "assistant",
        "content": f"Rescheduling {event.get('summary', 'event')}. When would you like to move it to?"
    })
    st.rerun()

# Sidebar configuration
with st.sidebar:
    st.title("⚙️ Settings")
    
    # User preferences
    st.session_state.user_settings["timezone"] = st.selectbox(
        "Timezone",
        ["UTC", "US/Pacific", "Europe/London", "Asia/Tokyo"],
        index=0
    )
    
    # LLM status panel
    st.markdown("---")
    st.markdown("### System Status")
    status = st.empty()
    
    # Debug panel
    if st.checkbox("Show Debug Info"):
        st.markdown("---")
        st.markdown("### Session Context")
        st.json(st.session_state.context)
        st.markdown("### Messages")
        st.json(st.session_state.messages)

# Main chat interface
st.title("🌟 Elite Calendar Assistant")
st.caption("Your AI-powered scheduling companion")

# Display chat messages
for i, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"], avatar="🧑‍💻" if message["role"] == "user" else "🤖"):
        st.markdown(message["content"])
        
        if message.get("details"):
            with st.expander("🔍 Details"):
                st.json(message["details"])
        
        if message.get("suggestions"):
            st.markdown("### Suggested Next Steps")
            for suggestion in message["suggestions"]:
                if st.button(suggestion):
                    st.session_state.messages.append({
                        "role": "user",
                        "content": suggestion
                    })
                    st.rerun()

# Chat input using chat_input instead of form
prompt = st.chat_input("How can I help you today?")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    
    with st.spinner("🤖 Processing your request..."):
        try:
            start_time = time.time()
            response = run_agent(prompt)
            processing_time = time.time() - start_time
            
            st.session_state.llm_status = "active" if response.get("llm_used", False) else "fallback"
            
            with st.chat_message("assistant"):
                if response.get("error"):
                    st.markdown(f'<div class="error-message">{response["output"]}</div>', unsafe_allow_html=True)
                else:
                    st.markdown(response["output"])
                
                if response.get("intermediate_steps"):
                    with st.expander("⚙️ Technical Details"):
                        st.json(response.get("intermediate_steps"))
                
                st.caption(f"Processed in {processing_time:.2f}s")
                
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": response["output"],
                    "details": response.get("intermediate_steps"),
                    "suggestions": response.get("suggestions", [])
                })
                
                if response.get("needs_confirmation"):
                    col1, col2 = st.columns(2)

                    with col1:
                        if st.button("✅ Confirm"):
                            try:
                                st.write("📤 Sending booking details:", response["booking_details"])
                                res = requests.post(
                                   f"{os.getenv('BACKEND_URL', 'http://127.0.0.1:8000')}/events",
                                   json=response["booking_details"],
                                   timeout=15  # slightly increased timeout
                )
                                if res.status_code == 200:
                                    booking_result = res.json()
                                    st.success("✅ Booking confirmed!")
                                    st.balloons()
                                    st.write("📅 Event ID:", booking_result.get("event_id"))
                                    st.write("🔗 View in calendar:", booking_result.get("html_link", "Link not available"))
                                    st.session_state.messages.append({
                                        "role": "assistant",
                                        "content": f"✅ Booking confirmed!\n\n📅 {booking_result.get('event_id')}\n🔗 {booking_result.get('html_link') or 'No link'}"
                                    })
                                    st.rerun()
                                else:
                                    st.error(f"❌ Booking failed with status: {res.status_code}")
                                    st.json(res.json())
                            except Exception as e:
                                st.error(f"❌ Failed to confirm booking: {str(e)}")
                                logger.error(f"❌ Confirm failed: {str(e)}")
                    with col2:
                        if st.button("✏️ Edit"):
                            st.session_state.messages.append({
                                "role": "assistant",
                                "content": "What would you like to change?"
                            })
                            st.rerun()
        except Exception as e:
            st.error(f"An error occurred: {str(e)}")
            logger.error(f"Processing error: {str(e)}")

# Upcoming events section
st.markdown("---")
st.subheader("📅 Your Upcoming Schedule")

tab1, tab2 = st.tabs(["This Week", "Next Week"])
with tab1:
    events = get_upcoming_events(7)
    if events:
        for i, event in enumerate(events[:5]):  # ✅ Fix 1: Use enumerate()
            with st.container():
                st.markdown(f"""
                **{event.get('summary', 'Meeting')}**  
                📅 {event['start']} - {event['end']}  
                👥 {', '.join(event.get('attendees', ['No attendees']))}
                """)
                if st.button("Reschedule", key=f"reschedule_{i}"):  # ✅ Fix 2: Use index as key
                    handle_reschedule_init(event)

    else:
        st.info("No events scheduled this week")

with tab2:
    events = get_upcoming_events(14)[5:10]
    if events:
        for event in events:
            with st.container():
                st.markdown(f"""
                **{event.get('summary', 'Meeting')}**  
                📅 {event['start']} - {event['end']}  
                👥 {', '.join(event.get('attendees', ['No attendees']))}
                """)
    else:
        st.info("No events scheduled next week")

# Status indicator update
status.markdown(f"""
**System Status:**  
{"🟢 AI Active" if st.session_state.llm_status == "active" else "🟠 Rule-Based Fallback"}  
**Timezone:** {st.session_state.user_settings['timezone']}  
**Last Updated:** {datetime.now().strftime('%H:%M:%S')}
""")