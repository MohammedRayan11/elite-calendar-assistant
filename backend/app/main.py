from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pydantic import BaseModel, validator
from datetime import datetime, timedelta
import os
from typing import Optional
from slowapi import Limiter
from slowapi.util import get_remote_address
import logging
from dotenv import load_dotenv
load_dotenv()

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Google Calendar Booking API",
    description="API for managing calendar appointments",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None
)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load configuration from environment
GOOGLE_CREDENTIALS = "C:/Users/mohammed rayan/Documents/assignment/assignment/backend/service-accounts.json"
CALENDAR_ID = os.getenv("CALENDAR_ID")
print("✅ Using Google Credentials File:", GOOGLE_CREDENTIALS)

# Pydantic models
class EventRequest(BaseModel):
    summary: str
    start_time: str
    end_time: str
    attendee_email: Optional[str] = None
    timezone: str = "UTC"
    description: Optional[str] = None
    location: Optional[str] = None

    @validator('start_time', 'end_time')
    def validate_iso_format(cls, v):
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("Time must be in ISO 8601 format")
        return v

class AvailabilityRequest(BaseModel):
    start_time: str
    end_time: str
    duration_minutes: int = 30

class EventResponse(BaseModel):
    event_id: str
    status: str
    html_link: Optional[str] = None

# Initialize Google Calendar service
def get_calendar_service():
    try:
        print(f"✅ Loading credentials from: {GOOGLE_CREDENTIALS}")
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Calendar service error: {str(e)}")
        raise HTTPException(status_code=500, detail="Calendar service unavailable")

# Health check endpoint
@app.get("/health")
async def health_check():
    try:
        service = get_calendar_service()
        service.calendars().get(calendarId=CALENDAR_ID).execute()
        return {"status": "healthy"}
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        raise HTTPException(status_code=503, detail="Service unavailable")

# Create event endpoint
@app.post("/events", response_model=EventResponse)
@limiter.limit("10/minute")
async def create_event(request: Request, event: EventRequest):
    """Create a new calendar event"""
    service = get_calendar_service()
    
    event_body = {
        'summary': event.summary,
        'start': {'dateTime': event.start_time, 'timeZone': event.timezone},
        'end': {'dateTime': event.end_time, 'timeZone': event.timezone},
    }
    
    if event.attendee_email:
        event_body['attendees'] = [{'email': event.attendee_email}]
    if event.description:
        event_body['description'] = event.description
    if event.location:
        event_body['location'] = event.location
    
    try:
        created_event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_body,
            sendUpdates='all' if event.attendee_email else 'none'
        ).execute()
        
        return {
            "event_id": created_event['id'],
            "status": "scheduled",
            "html_link": created_event.get('htmlLink')
        }
    except Exception as e:
        logger.error(f"Event creation failed: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# Check availability endpoint
@app.get("/availability")
@limiter.limit("30/minute")
async def check_availability(
    request: Request,
    start_time: str,
    end_time: str,
    page_token: Optional[str] = None
):
    """Check available time slots"""
    service = get_calendar_service()
    
    try:
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy='startTime',
            pageToken=page_token
        ).execute()
        
        busy_slots = [
            {
                'start': event['start'].get('dateTime', event['start'].get('date')),
                'end': event['end'].get('dateTime', event['end'].get('date')),
                'summary': event.get('summary', 'Busy')
            }
            for event in events_result.get('items', [])
        ]
        
        return {
            "busy_slots": busy_slots,
            "next_page_token": events_result.get('nextPageToken'),
            "time_zone": events_result.get('timeZone', 'UTC')
        }
    except Exception as e:
        logger.error(f"Availability check failed: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# Suggest slots endpoint
@app.get("/suggest-slots")
@limiter.limit("20/minute")
async def suggest_slots(
    request: Request,
    start_time: str,
    end_time: str,
    duration_minutes: int = 30,
    increment_minutes: int = 15
):
    """Suggest available time slots"""
    service = get_calendar_service()
    
    try:
        # Get busy slots
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        busy_slots = [
            (event['start'].get('dateTime', event['start'].get('date')),
            event['end'].get('dateTime', event['end'].get('date'))
        ) for event in events_result.get('items', [])]
        
        # Generate suggestions
        suggestions = []
        current_time = datetime.fromisoformat(start_time)
        end_time_dt = datetime.fromisoformat(end_time)
        duration = timedelta(minutes=duration_minutes)
        increment = timedelta(minutes=increment_minutes)
        
        while current_time + duration <= end_time_dt:
            slot_end = current_time + duration
            conflict = False
            
            for busy_start, busy_end in busy_slots:
                busy_start_dt = datetime.fromisoformat(busy_start)
                busy_end_dt = datetime.fromisoformat(busy_end)
                
                if not (slot_end <= busy_start_dt or current_time >= busy_end_dt):
                    conflict = True
                    current_time = busy_end_dt  # Jump to after busy slot
                    break
                    
            if not conflict:
                suggestions.append({
                    "start": current_time.isoformat(),
                    "end": slot_end.isoformat(),
                    "duration_minutes": duration_minutes
                })
                current_time += increment
            else:
                current_time += increment
        
        return {"suggestions": suggestions}
    except Exception as e:
        logger.error(f"Slot suggestion failed: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# Event details endpoint
@app.get("/events/{event_id}")
@limiter.limit("15/minute")
async def get_event(request: Request, event_id: str):
    """Get event details"""
    service = get_calendar_service()
    
    try:
        event = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=event_id
        ).execute()
        
        return {
            "summary": event.get('summary'),
            "start": event['start'],
            "end": event['end'],
            "status": event.get('status'),
            "attendees": event.get('attendees', []),
            "htmlLink": event.get('htmlLink')
        }
    except Exception as e:
        logger.error(f"Event retrieval failed: {str(e)}")
        raise HTTPException(status_code=404, detail="Event not found")

# Cancel event endpoint
@app.delete("/events/{event_id}")
@limiter.limit("10/minute")
async def cancel_event(request: Request, event_id: str):
    """Cancel a calendar event"""
    service = get_calendar_service()
    
    try:
        service.events().delete(
            calendarId=CALENDAR_ID,
            eventId=event_id,
            sendUpdates='all'
        ).execute()
        
        return {"status": "cancelled", "event_id": event_id}
    except Exception as e:
        logger.error(f"Event cancellation failed: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/")
async def root():
    return {"message": "API is live. Go to /docs for Swagger UI."}
