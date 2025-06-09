from datetime import datetime, timedelta
from googleapiclient.discovery import build
from dateutil.parser import parse
import pytz
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def build_calendar_service(credentials):
    try:
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        logger.error(f"Error building calendar service: {str(e)}")
        raise

def find_available_slots(credentials, duration_minutes, preferred_time_range):
    try:
        logger.debug(f"Finding available slots with duration: {duration_minutes} minutes")
        logger.debug(f"Preferred time range: {preferred_time_range}")
        
        service = build_calendar_service(credentials)
        
        calendar_list = service.calendarList().get(calendarId='primary').execute()
        timezone = calendar_list.get('timeZone', 'UTC')
        local_tz = pytz.timezone(timezone)
        logger.debug(f"Using timezone: {timezone}")
        
        if not preferred_time_range:
            now = datetime.now(local_tz)
            start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
            end_time = (now + timedelta(days=7)).replace(hour=17, minute=0, second=0, microsecond=0)
        else:
            start_time = preferred_time_range['start'].astimezone(local_tz)
            end_time = preferred_time_range['end'].astimezone(local_tz)
        
        logger.debug(f"Search period: {start_time} to {end_time}")
        
        body = {
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "items": [{"id": "primary"}]
        }
        
        logger.debug("Querying calendar API for busy periods")
        events_result = service.freebusy().query(body=body).execute()
        logger.debug(f"Calendar API response: {events_result}")
        
        calendars = events_result.get('calendars', {})
        busy_periods = calendars.get('primary', {}).get('busy', [])
        logger.debug(f"Found {len(busy_periods)} busy periods")
        
        available_slots = []
        current_time = start_time
        
        while current_time < end_time:
            if 9 <= current_time.hour < 17:
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