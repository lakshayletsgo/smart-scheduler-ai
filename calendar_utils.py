from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dateutil.parser import parse
from datetime import datetime, timedelta
import pytz
import logging
import os
import pickle

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.pickle'

class CalendarManager:
    def __init__(self):
        self.creds = None
        self.service = None
        self._load_credentials()
        
    def _load_credentials(self):
        """Load or refresh Google Calendar credentials"""
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'rb') as token:
                self.creds = pickle.load(token)
                
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                self.creds = flow.run_local_server(port=5000)
                
            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(self.creds, token)
                
        self.service = build('calendar', 'v3', credentials=self.creds)
        
    async def schedule_appointment(self, name: str, date: str, time: str, reason: str) -> bool:
        """Schedule an appointment in Google Calendar"""
        try:
            # Combine date and time into a datetime object
            dt_str = f"{date} {time}"
            start_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            end_time = start_time + timedelta(hours=1)  # Default 1-hour appointments
            
            timezone = pytz.timezone('UTC')  # You can change this to your local timezone
            
            event = {
                'summary': f"Appointment with {name}",
                'description': reason,
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': str(timezone),
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': str(timezone),
                },
                'attendees': [
                    {'email': name if '@' in name else ''}  # Add email if provided
                ],
                'reminders': {
                    'useDefault': True
                }
            }
            
            # Check if the time slot is available
            busy_slots = self.service.freebusy().query(
                body={
                    'timeMin': start_time.isoformat(),
                    'timeMax': end_time.isoformat(),
                    'items': [{'id': 'primary'}]
                }
            ).execute()
            
            # If the time slot is busy, return False
            if busy_slots['calendars']['primary']['busy']:
                return False
                
            # Create the event
            event = self.service.events().insert(
                calendarId='primary',
                body=event
            ).execute()
            
            return True
            
        except Exception as e:
            print(f"Error scheduling appointment: {str(e)}")
            return False
            
    def get_available_slots(self, date: str) -> list:
        """Get available time slots for a given date"""
        try:
            # Convert date string to datetime
            target_date = datetime.strptime(date, "%Y-%m-%d")
            start_of_day = target_date.replace(hour=9)  # Start at 9 AM
            end_of_day = target_date.replace(hour=17)  # End at 5 PM
            
            # Get busy periods
            busy_slots = self.service.freebusy().query(
                body={
                    'timeMin': start_of_day.isoformat() + 'Z',
                    'timeMax': end_of_day.isoformat() + 'Z',
                    'items': [{'id': 'primary'}]
                }
            ).execute()
            
            # Create list of all possible hour slots
            all_slots = []
            current = start_of_day
            while current < end_of_day:
                all_slots.append(current.strftime("%H:%M"))
                current += timedelta(hours=1)
                
            # Remove busy slots
            busy_periods = busy_slots['calendars']['primary']['busy']
            available_slots = all_slots.copy()
            
            for period in busy_periods:
                busy_start = datetime.fromisoformat(period['start'].replace('Z', '+00:00'))
                busy_start_str = busy_start.strftime("%H:%M")
                if busy_start_str in available_slots:
                    available_slots.remove(busy_start_str)
                    
            return available_slots
            
        except Exception as e:
            print(f"Error getting available slots: {str(e)}")
            return []

def build_calendar_service(credentials):
    try:
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        logger.error(f"Error building calendar service: {str(e)}")
        raise

def find_available_slots(credentials, start_time=None, duration_minutes=30, attendees=None):
    try:
        logger.debug(f"Finding available slots with duration: {duration_minutes} minutes")
        logger.debug(f"Start time: {start_time}")
        logger.debug(f"Attendees: {attendees}")
        
        service = build_calendar_service(credentials)
        
        calendar_list = service.calendarList().get(calendarId='primary').execute()
        timezone = calendar_list.get('timeZone', 'UTC')
        local_tz = pytz.timezone(timezone)
        logger.debug(f"Using timezone: {timezone}")
        
        if not start_time:
            now = datetime.now(local_tz)
            start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
            end_time = (now + timedelta(days=7)).replace(hour=17, minute=0, second=0, microsecond=0)
        else:
            start_time = start_time.astimezone(local_tz)
            end_time = (start_time + timedelta(days=7)).replace(hour=17, minute=0, second=0, microsecond=0)
        
        logger.debug(f"Search period: {start_time} to {end_time}")
        
        # Build the query for free/busy info
        query_items = [{"id": "primary"}]
        if attendees:
            for email in attendees:
                query_items.append({"id": email})
        
        body = {
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "items": query_items
        }
        
        logger.debug("Querying calendar API for busy periods")
        events_result = service.freebusy().query(body=body).execute()
        logger.debug(f"Calendar API response: {events_result}")
        
        # Combine busy periods from all calendars
        busy_periods = []
        for calendar_id, calendar_info in events_result.get('calendars', {}).items():
            busy_periods.extend(calendar_info.get('busy', []))
        
        logger.debug(f"Found {len(busy_periods)} total busy periods")
        
        available_slots = []
        current_time = start_time
        
        while current_time < end_time:
            if 9 <= current_time.hour < 17:  # Only check during business hours
                slot_end = current_time + timedelta(minutes=duration_minutes)
                is_free = True
                
                for busy in busy_periods:
                    busy_start = parse(busy['start']).astimezone(local_tz)
                    busy_end = parse(busy['end']).astimezone(local_tz)
                    
                    if (current_time < busy_end and slot_end > busy_start):
                        is_free = False
                        current_time = busy_end
                        break
                
                if is_free:
                    available_slots.append(current_time)
                    current_time += timedelta(minutes=30)
                else:
                    continue
            
            if current_time.hour >= 17:
                current_time = (current_time + timedelta(days=1)).replace(hour=9, minute=0)
            else:
                current_time += timedelta(minutes=30)
        
        logger.debug(f"Found {len(available_slots)} available slots")
        return available_slots
        
    except Exception as e:
        logger.error(f"Error finding available slots: {str(e)}", exc_info=True)
        raise

def schedule_meeting(credentials, start_time, duration, attendees, purpose):
    try:
        logger.debug(f"Scheduling meeting:")
        logger.debug(f"Start time: {start_time}")
        logger.debug(f"Duration: {duration} minutes")
        logger.debug(f"Attendees: {attendees}")
        logger.debug(f"Purpose: {purpose}")
        
        service = build_calendar_service(credentials)
        
        end_time = start_time + timedelta(minutes=duration)
        
        event = {
            'summary': purpose,
            'description': purpose,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': start_time.tzinfo.zone,
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': end_time.tzinfo.zone,
            },
            'attendees': [{'email': email} for email in attendees],
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 24 * 60},
                    {'method': 'popup', 'minutes': 30},
                ]
            },
        }
        
        logger.debug("Creating calendar event")
        event = service.events().insert(calendarId='primary', body=event, sendUpdates='all').execute()
        meeting_link = event.get('htmlLink', '')
        logger.debug(f"Event created successfully. Link: {meeting_link}")
        
        success_message = (
            f"✅ Meeting scheduled successfully!\n\n"
            f"📝 Details:\n"
            f"• Title: {purpose}\n"
            f"• Date: {start_time.strftime('%A, %B %d, %Y')}\n"
            f"• Time: {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}\n"
            f"• Duration: {duration} minutes\n"
            f"• Attendees: {', '.join(attendees)}\n\n"
            f"📧 Email invitations have been sent to all attendees\n"
            f"🔗 View in Calendar: {meeting_link}"
        )
        
        return True, success_message
    except Exception as e:
        error_message = f"Error scheduling meeting: {str(e)}"
        logger.error(error_message, exc_info=True)
        return False, error_message

def create_calendar_event(credentials, summary, start_time, attendees, duration_minutes=30):
    """Create a calendar event and send invites to attendees.
    
    Args:
        credentials: Google Calendar credentials
        summary: Event title/summary
        start_time: Start time as datetime object
        attendees: List of attendee email addresses
        duration_minutes: Duration of meeting in minutes (default 30)
    
    Returns:
        Created event object or None if there was an error
    """
    try:
        service = build_calendar_service(credentials)
        
        # Get calendar timezone
        calendar_list = service.calendarList().get(calendarId='primary').execute()
        timezone = calendar_list.get('timeZone', 'UTC')
        
        # Convert start_time to calendar timezone if needed
        if start_time.tzinfo is None:
            local_tz = pytz.timezone(timezone)
            start_time = local_tz.localize(start_time)
        
        end_time = start_time + timedelta(minutes=duration_minutes)
        
        # Format attendees
        attendee_list = [{'email': email} for email in attendees]
        
        event = {
            'summary': summary,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': timezone,
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': timezone,
            },
            'attendees': attendee_list,
            'reminders': {
                'useDefault': True
            }
        }
        
        # Check if the time slot is available
        query_items = [{"id": "primary"}]
        for email in attendees:
            query_items.append({"id": email})
            
        busy_result = service.freebusy().query(
            body={
                "timeMin": start_time.isoformat(),
                "timeMax": end_time.isoformat(),
                "items": query_items
            }
        ).execute()
        
        # Check if anyone is busy
        for calendar_id, calendar_info in busy_result.get('calendars', {}).items():
            if calendar_info.get('busy', []):
                logger.warning(f"Time slot is busy for {calendar_id}")
                return None
        
        # Create the event
        event = service.events().insert(
            calendarId='primary',
            body=event,
            sendUpdates='all'  # Send email notifications to attendees
        ).execute()
        
        logger.debug(f"Event created: {event.get('htmlLink')}")
        return event
        
    except Exception as e:
        logger.error(f"Error creating calendar event: {str(e)}")
        return None