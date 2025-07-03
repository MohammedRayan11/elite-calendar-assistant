import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, Tool, create_react_agent
from langchain_core.prompts import ChatPromptTemplate
from langchain.memory import ConversationBufferMemory
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import re
import logging
import json
from functools import lru_cache
from tenacity import retry, stop_after_attempt, wait_exponential
import dateparser
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

class CalendarAgent:
    def __init__(self):
        self.llm = self._initialize_llm()
        self.tools = self._initialize_tools()
        self.memory = ConversationBufferMemory(memory_key="chat_history")
        self.agent_executor = self._initialize_agent()
        self.backend_url = os.getenv('BACKEND_URL', 'http://127.0.0.1:8000')
        self._validate_config()
        self.pending_booking = None

    def _initialize_llm(self) -> Optional[Any]:
        """Initialize Gemini LLM (Google Generative AI)"""
        if os.getenv("GEMINI_API_KEY"):
            try:
                return ChatGoogleGenerativeAI(
                    model="models/gemini-pro",
                    google_api_key=os.getenv("GEMINI_API_KEY"),
                    temperature=0.3
                )
            except Exception as e:
                logger.error(f"Gemini initialization failed: {str(e)}")
                return None

        logger.error("No valid Gemini API key found.")
        return None


    def _validate_config(self) -> None:
        """Validate required configuration"""
        if not self.backend_url:
            raise ValueError("BACKEND_URL environment variable not set")
        if not self.llm and not os.getenv("RULE_BASED_FALLBACK_ENABLED", "true").lower() == "true":
            raise RuntimeError("No LLM available and rule-based fallback disabled")

    def _initialize_tools(self) -> List[Tool]:
        """Initialize calendar tools with enhanced descriptions"""
        return [
            Tool(
                name="AvailabilityChecker",
                func=self._check_availability,
                description="Check available time slots between given dates. Input should be a JSON string with 'start_time' and 'end_time' in ISO-8601 format."
            ),
            Tool(
                name="AppointmentBooker",
                func=self._book_appointment,
                description="Book an appointment. Requires JSON input with 'summary', 'start_time', 'end_time' (ISO-8601), and optional 'attendee_email'."
            ),
            Tool(
                name="SlotSuggester",
                func=self._suggest_slots,
                description="Suggest available time slots. Requires JSON input with 'start_time', 'end_time' (ISO-8601), and 'duration_minutes'."
            )
        ]

    def _initialize_agent(self) -> Optional[AgentExecutor]:
        """Initialize agent with current best practices"""
        if not self.llm:
            return None
            
        try:
            prompt = ChatPromptTemplate.from_messages([
                ("human", """You are an expert calendar assistant. Help users manage their schedules.
                
                You have access to the following tools:
                {tools}
                
                Use the following format:
                
                Question: the input question you must answer
                Thought: you should always think about what to do
                Action: the action to take, should be one of [{tool_names}]
                Action Input: the input to the action
                Observation: the result of the action
                ... (this Thought/Action/Action Input/Observation can repeat N times)
                Thought: I now know the final answer
                Final Answer: the final answer to the original input question
                
                User Question: {input}"""),
                    ("ai", "{agent_scratchpad}")
    
            ])
            
            agent = create_react_agent(
                llm=self.llm,
                tools=self.tools,
                prompt=prompt
            )
            
            return AgentExecutor(
                agent=agent,
                tools=self.tools,
                memory=self.memory,
                handle_parsing_errors=True,
                max_iterations=5,
                verbose=True
            )
        except Exception as e:
            logger.error(f"Agent initialization failed: {str(e)}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def _call_backend(self, endpoint: str, method: str = "GET", params: dict = None, json_data: dict = None) -> Dict[str, Any]:
        """Robust backend calls with retry logic"""
        try:
            url = f"{self.backend_url}/{endpoint}"
            response = requests.request(
                method,
                url,
                params=params,
                json=json_data,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Backend call failed: {str(e)}")
            raise

    @lru_cache(maxsize=32)
    def _check_availability(self, query: str) -> List[Dict[str, str]]:
        """Check available time slots with caching"""
        try:
            params = json.loads(query)
            return self._call_backend(
                "availability",
                params={
                    "start_time": params["start_time"],
                    "end_time": params["end_time"]
                }
            ).get("busy_slots", [])
        except Exception as e:
            logger.error(f"Availability check failed: {str(e)}")
            return []

    def _book_appointment(self, query: str) -> Dict[str, str]:
        """Book an appointment with robust error handling"""
        try:
            booking_data = json.loads(query)
            required_fields = ["summary", "start_time", "end_time"]
            if not all(field in booking_data for field in required_fields):
                raise ValueError("Missing required booking fields")
                
            return self._call_backend(
                "events",
                method="POST",
                json_data={
                    "summary": booking_data["summary"],
                    "start_time": booking_data["start_time"],
                    "end_time": booking_data["end_time"],
                    "attendee_email": booking_data.get("attendee_email")
                }
            )
        except Exception as e:
            logger.error(f"Booking failed: {str(e)}")
            return {
                "error": "Failed to book appointment",
                "details": str(e),
                "user_message": "Sorry, I couldn't book your meeting. Please try again later."
            }

    def _suggest_slots(self, query: str) -> List[Dict[str, str]]:
        """Suggest available time slots"""
        try:
            params = json.loads(query)
            return self._call_backend(
                "suggest-slots",
                params={
                    "start_time": params["start_time"],
                    "end_time": params["end_time"],
                    "duration_minutes": params.get("duration_minutes", 30)
                }
            ).get("suggestions", [])
        except Exception as e:
            logger.error(f"Slot suggestion failed: {str(e)}")
            return []

    def _handle_booking_followup(self, query: str) -> Dict[str, Any]:
        """Process follow-up responses for meeting booking"""
        if not self.pending_booking:
            return self._rule_based_fallback(query)
            
        current_step = self.pending_booking['step']
        current_field = self.pending_booking['required_info'][current_step-1][0]
        
        # Store the user's response
        self.pending_booking['collected_info'][current_field] = query
        
        # Check if we have all required information
        if current_step >= len(self.pending_booking['required_info']):
            # All info collected - confirm booking
            booking_details = self._prepare_booking_details()
            self.pending_booking = None  # Clear the pending booking
            
            return {
                "output": f"Ready to book:\n{self._format_booking_summary(booking_details)}\n\n✅ Confirm or ✏️ Edit?",
                "booking_details": booking_details,
                "needs_confirmation": True,
                "llm_used": False
            }
        
        # Ask for next piece of information
        self.pending_booking['step'] += 1
        next_question = self.pending_booking['required_info'][current_step][1]
        
        return {
            "output": next_question,
            "needs_followup": True,
            "llm_used": False
        }

    def _to_iso_datetime(self, date_str: str, time_str: str) -> str:
        try:
            full_input = f"{date_str} {time_str}"
            dt = dateparser.parse(full_input)
            if dt is None:
                raise ValueError(f"Could not parse: {full_input}")
            return dt.isoformat() + "Z"
        except Exception as e:
            logger.warning(f"Failed to parse datetime: {e}")
            return (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"
    def _prepare_booking_details(self) -> Dict[str, Any]:
        """Convert collected info into booking parameters"""
        if not self.pending_booking:
            return {}
            
        info = self.pending_booking['collected_info']
        return {
            'summary': info.get('meeting_title', 'Meeting'),
            'start_time': self._to_iso_datetime(info.get('date'), info.get('time')),
            'end_time': self._calculate_end_time(info.get('date', ''), info.get('time', ''), info.get('duration', '1 hour')),
            'attendee_email': info.get('attendees', '')
        }

    def _calculate_end_time(self, date_str: str, time_str: str, duration_str: str) -> str:
        """Calculate end time based on duration"""
        try:
            # Parse duration (e.g., "1 hour" or "30 mins")
            if "hour" in duration_str:
                hours = int(duration_str.split()[0])
                delta = timedelta(hours=hours)
            else:
                minutes = int(duration_str.split()[0])
                delta = timedelta(minutes=minutes)
                
            start_time = datetime.fromisoformat(date_str + 'T' + time_str)
            return (start_time + delta).isoformat() + 'Z'
        except Exception:
            logger.warning("Failed to calculate end time, using default 1 hour duration")
            return (datetime.now() + timedelta(hours=1)).isoformat() + 'Z'

    def _format_booking_summary(self, details: Dict[str, Any]) -> str:
        """Format booking details for user confirmation"""
        return f"""
📅 {details.get('summary', 'Meeting')}
🗓️ {details.get('start_time', 'Unknown time')}
⏱️ Duration: {details.get('duration', '1 hour')}
👥 Attendee: {details.get('attendee_email', 'None specified')}
"""

    def _rule_based_fallback(self, query: str) -> Dict[str, Any]:
        """Enhanced rule-based processing with better intent detection"""
        query = query.lower()
        
        # Time extraction patterns
        time_pattern = r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)'
        times = re.findall(time_pattern, query)
        date_keywords = ['today', 'tomorrow', 'monday', 'tuesday', 'wednesday', 
                        'thursday', 'friday', 'saturday', 'sunday']
        
        # Check for booking intent
        if any(word in query for word in ['book', 'schedule', 'meeting', 'appointment']):
            response = {
                "output": "I can help book that meeting. ",
                "intermediate_steps": [],
                "llm_used": False
            }
            
            # Check for missing information
            missing = []
            if not any(word in query for word in date_keywords + ['today', 'tomorrow']):
                missing.append("date (e.g., 'tomorrow' or 'Friday')")
            if len(times) < 2:
                missing.append("start and end time (e.g., '2pm to 3pm')")
            
            if missing:
                response["output"] += "Please provide:\n- " + "\n- ".join(missing)
            else:
                response["output"] += "Should I book this meeting?"
                response["needs_confirmation"] = True
                
            return response
        
        # Check for availability intent
        elif any(word in query for word in ['available', 'free', 'busy']):
            return {
                "output": "I can check availability. Please specify:\n"
                         "- Date (e.g., 'Friday')\n"
                         "- Time range (e.g., '9am to 5pm')",
                "intermediate_steps": [],
                "llm_used": False
            }
        
        # Default response
        return {
            "output": "I can help with:\n"
                     "- Booking meetings\n"
                     "- Checking availability\n"
                     "- Managing calendar events\n\n"
                     "Try commands like:\n"
                     "'Book team meeting tomorrow at 2pm for 1 hour'\n"
                     "'What's my availability on Friday?'",
            "intermediate_steps": [],
            "llm_used": False
        }

    def run_agent(self, query: str) -> Dict[str, Any]:
        """
        Process user query and return response.
        
        Args:
            query: User's natural language input
            
        Returns:
            Dictionary containing:
            - output: Text response to show user
            - intermediate_steps: Debugging info
            - llm_used: Boolean indicating if LLM was used
            - needs_confirmation: Boolean if confirmation needed
            - booking_details: Dictionary if booking pending
            - error: Optional error message
        """
        try:
            # Check if we're in the middle of a booking flow
            if self.pending_booking:
                return self._handle_booking_followup(query)
            
            # Detect booking intent
            if any(word in query.lower() for word in ['book', 'schedule', 'meeting', 'appointment']):
                self.pending_booking = {
                    'step': 1,
                    'collected_info': {},
                    'required_info': [
                        ('meeting_title', "What's the meeting about?"),
                        ('date', "What date should we meet? (e.g. tomorrow, Friday)"),
                        ('time', "What time? (e.g. 2pm, 11:30 AM)"),
                        ('duration', "How long should it be? (e.g. 1 hour, 30 mins)"),
                        ('attendees', "Any attendees? (comma-separated emails)")
                    ]
                }
                return {
                    "output": "I'll help schedule that meeting. " + self.pending_booking['required_info'][0][1],
                    "needs_followup": True,
                    "llm_used": False
                }
            
            # Try using LLM agent if available
            if self.agent_executor:
                result = self.agent_executor.invoke({"input": query})
                return {
                    "output": result.get("output", "I didn't get a response"),
                    "intermediate_steps": result.get("intermediate_steps", []),
                    "llm_used": True
                }
            
            # Fallback to rule-based processing
            return self._rule_based_fallback(query)
            
        except Exception as e:
            logger.error(f"Agent processing failed: {str(e)}")
            return {
                "output": "Sorry, I encountered an error processing your request. Please try again.",
                "error": str(e),
                "llm_used": False
            }

# Singleton instance
calendar_agent = CalendarAgent()

def run_agent(query: str) -> Dict[str, Any]:
    """Public interface to run the calendar agent"""
    return calendar_agent.run_agent(query)