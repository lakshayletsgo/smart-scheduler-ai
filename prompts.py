from google.generativeai import GenerativeModel
import google.generativeai as genai
import re
from datetime import datetime, timedelta
import dateparser
import os
import streamlit as st
import calendar_utils
import logging

# Initialize Gemini
genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))
model = genai.GenerativeModel('gemini-2.0-flash')

SYSTEM_PROMPT = """You are a helpful AI scheduling assistant. Your goal is to help users schedule meetings by understanding their requirements and preferences through natural conversation.

Follow these rules:
1. If you already have the information, DO NOT ask for it again
2. Only ask for missing information in this order:
   - Meeting purpose (if not provided)
   - Duration (if not provided, default to 30 minutes)
   - Preferred time/date (if not provided)
   - Attendees' emails (if not provided)

3. Once you have all required information:
   - Confirm the details
   - Proceed to check calendar availability

4. After showing available slots:
   - Wait for the user to select a slot
   - Don't ask for any information again

Required information status:
{info_status}

Previous context:
{context}"""

logger = logging.getLogger(__name__)

def extract_time_expression(text):
    """Parse natural language time expressions into structured datetime objects."""
    # Try to parse the entire expression first
    parsed_time = dateparser.parse(text, settings={
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': datetime.now()
    })
    if parsed_time and isinstance(parsed_time, datetime):
        return parsed_time
    
    # Handle relative time expressions
    relative_patterns = {
        r'late next week': lambda: datetime.now() + timedelta(days=10),
        r'early next week': lambda: datetime.now() + timedelta(days=7),
        r'end of day': lambda: datetime.now().replace(hour=17, minute=0),
        r'tomorrow morning': lambda: (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0),
        r'tomorrow afternoon': lambda: (datetime.now() + timedelta(days=1)).replace(hour=13, minute=0),
    }
    
    for pattern, time_func in relative_patterns.items():
        if re.search(pattern, text.lower()):
            try:
                result = time_func()
                if isinstance(result, datetime):
                    return result
            except Exception as e:
                logger.error(f"Error processing relative time pattern {pattern}: {e}")
                continue
    
    return None

def extract_duration(text):
    """Extract meeting duration from text."""
    patterns = {
        r'(\d+)\s*hour': lambda x: int(x) * 60,
        r'(\d+)\s*hr': lambda x: int(x) * 60,
        r'(\d+)\s*min': lambda x: int(x),
        r'half\s*hour': lambda x: 30,
        r'quarter\s*hour': lambda x: 15,
    }
    
    for pattern, duration_func in patterns.items():
        match = re.search(pattern, text.lower())
        if match:
            return duration_func(match.group(1) if match.groups() else None)
    
    # Default to 30 minutes if no duration specified
    return 30

def extract_emails(text):
    """Extract email addresses from text."""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.findall(email_pattern, text)

def get_info_status(state):
    """Get the status of required information."""
    return {
        "purpose": "✅ Set" if state.purpose else "❌ Missing",
        "duration": "✅ Set" if state.meeting_duration else "❌ Missing",
        "time": "✅ Set" if state.preferred_time else "❌ Missing",
        "attendees": "✅ Set" if state.attendees else "❌ Missing"
    }

def format_debug_info(conversation_state):
    """Format debug information about the current conversation state"""
    return f"""Current conversation state:
• Purpose: {conversation_state.purpose}
• Duration: {conversation_state.meeting_duration} minutes
• Preferred time: {conversation_state.preferred_time}
• Attendees: {', '.join(conversation_state.attendees) if conversation_state.attendees else 'None'}
• Answered questions: {', '.join(conversation_state.answered_questions)}
• Slots shown: {conversation_state.slots_shown}
• Selected slot: {conversation_state.selected_slot}"""

def format_initial_greeting():
    """Format the initial greeting message"""
    return "━━━ Welcome! ━━━\n\n👋 Hello! I'm your AI scheduling assistant.\n\nI'll help you schedule your meeting. Let's get started!\n\nWhat's the purpose of your meeting?"

def format_info_request(info_type):
    """Format a request for specific information"""
    if info_type == 'purpose':
        return "What's the purpose of your meeting?"
    elif info_type == 'duration':
        return "How long would you like the meeting to be? (default is 30 minutes)"
    elif info_type == 'time':
        return "When would you like to schedule this meeting? You can say things like:\n" + \
               "• 'tomorrow morning'\n" + \
               "• 'next Monday at 2pm'\n" + \
               "• 'June 28 at 10am'"
    elif info_type == 'attendees':
        return "Who would you like to invite to this meeting? (Please provide email addresses)"
    return "Could you please provide more information about your meeting?"

def format_meeting_details(state):
    """Format the current meeting details"""
    if not any([state.purpose, state.meeting_duration, state.preferred_time, state.attendees]):
        return None
        
    details = ["Here's what I have so far:"]
    if state.purpose:
        details.append(f"📝 Purpose: {state.purpose}")
    if state.meeting_duration:
        details.append(f"⏱️ Duration: {state.meeting_duration} minutes")
    if state.preferred_time:
        try:
            start_time = dateparser.parse(state.preferred_time['start'])
            if start_time:
                details.append(f"📅 Time: {start_time.strftime('%A, %B %d at %I:%M %p')}")
        except Exception as e:
            logger.error(f"Error formatting preferred time: {e}")
    if state.attendees:
        details.append("👥 Attendees:")
        for attendee in state.attendees:
            details.append(f"  • {attendee}")
            
    return "\n".join(details)

def format_missing_info(missing_info):
    """Format the list of missing information"""
    if not missing_info:
        return None
        
    info_map = {
        'purpose': 'the meeting purpose',
        'duration': 'the meeting duration',
        'time': 'when you would like to meet',
        'attendees': 'who you would like to invite'
    }
    
    missing = [info_map.get(info, info) for info in missing_info]
    if len(missing) == 1:
        return f"\n\nI'll also need to know {missing[0]}."
    elif len(missing) == 2:
        return f"\n\nI'll also need to know {missing[0]} and {missing[1]}."
    else:
        last = missing.pop()
        return f"\n\nI'll also need to know {', '.join(missing)}, and {last}."

def format_available_slots(slots):
    """Format the list of available time slots"""
    if not slots:
        return "I couldn't find any available slots in the next week. Would you like to try a different time?"
        
    response = ["I found these available slots:"]
    for i, slot in enumerate(slots, 1):
        try:
            if isinstance(slot, str):
                slot = dateparser.parse(slot)
            if slot:
                response.append(f"{i}. {slot.strftime('%A, %B %d at %I:%M %p')}")
        except Exception as e:
            logger.error(f"Error formatting slot: {e}")
            continue
            
    response.append("\nWhich slot would you prefer? (enter the number)")
    return "\n".join(response)

def format_confirmation(state, slot_time):
    """Format the confirmation message"""
    return f"I found an available slot for your meeting: {slot_time}.\n\n" + \
           "Here's a summary of your meeting:\n" + \
           f"📝 Purpose: {state.purpose}\n" + \
           f"⏱️ Duration: {state.meeting_duration} minutes\n" + \
           f"👥 Attendees: {', '.join(state.attendees)}\n\n" + \
           "Should I go ahead and schedule this meeting? (yes/no)"

def format_success(state, slot_time):
    """Format the success message"""
    return f"✅ Perfect! I've scheduled the meeting:\n\n" + \
           f"📝 Purpose: {state.purpose}\n" + \
           f"📅 Time: {slot_time}\n" + \
           f"⏱️ Duration: {state.meeting_duration} minutes\n" + \
           "👥 Attendees:\n" + "\n".join([f"• {attendee}" for attendee in state.attendees]) + \
           "\n\nI've sent calendar invites to all attendees.\n\n" + \
           "Is there anything else I can help you with?"

def format_success_message(calendar_link):
    """Format the success message with proper line breaks"""
    lines = [
        "━━━ Meeting Scheduled! ━━━<br>",
        "<br>",
        "✅ Success! Your meeting has been scheduled.<br>",
        "<br>",
        "📨 Calendar invitations have been sent to all attendees.<br>",
        f"🔗 View in Calendar: {calendar_link}<br>",
        "<br>",
        "━━━ Anything Else? ━━━<br>",
        "<br>",
        "Is there anything else I can help you with?<br>",
        "<br>"
    ]
    return "\n".join(lines)

def format_error_message(error_type="general"):
    """Format error messages"""
    messages = {
        "no_credentials": """━━━ Authentication Required ━━━

🔒 Please sign in to your Google Calendar to continue.

I'll help you schedule the meeting once you're signed in.
""",
        "calendar_error": """━━━ Calendar Error ━━━

❌ There was an error accessing the calendar.

Please try again or check your calendar permissions.
""",
        "general": """━━━ Error ━━━

❌ Something went wrong.

Please try again or start over by typing "reset".
"""
    }
    return messages.get(error_type, messages["general"])

def get_ai_response(user_input, conversation_state):
    """Get AI response based on user input and conversation state"""
    # Format the system prompt with current state
    info_status = get_info_status(conversation_state)
    
    # Build context from conversation state
    context = []
    if conversation_state.purpose:
        context.append(f"Meeting purpose: {conversation_state.purpose}")
    if conversation_state.meeting_duration:
        context.append(f"Meeting duration: {conversation_state.meeting_duration} minutes")
    if conversation_state.preferred_time:
        context.append(f"Preferred time: {conversation_state.preferred_time}")
    if conversation_state.attendees:
        context.append(f"Attendees: {', '.join(conversation_state.attendees)}")
    if conversation_state.slots_shown:
        context.append("Available slots have been shown")
    if conversation_state.selected_slot:
        context.append(f"Selected slot: {conversation_state.selected_slot}")
    
    context_str = "\n".join(context) if context else "No previous context"
    
    # Get response from AI
    prompt = SYSTEM_PROMPT.format(
        info_status="\n".join([f"{k}: {v}" for k, v in info_status.items()]),
        context=context_str
    )
    
    try:
        response = model.generate_content([prompt, user_input])
        return response.text if response else None
    except Exception as e:
        logger.error(f"Error getting AI response: {e}")
        return None

def should_check_calendar(response):
    """Check if we should check calendar availability based on the response"""
    # Keywords that indicate we should check calendar
    calendar_keywords = [
        'available',
        'schedule',
        'book',
        'set up',
        'find',
        'check',
        'calendar',
        'slot',
        'time'
    ]
    
    # Convert response to lowercase for case-insensitive matching
    response = response.lower()
    
    # Check if any calendar keywords are in the response
    return any(keyword in response for keyword in calendar_keywords)

def get_next_question(state):
    """Get the next question to ask based on missing information"""
    if not state.purpose:
        return "What's the purpose of your meeting?"
    elif not state.meeting_duration:
        return "How long would you like the meeting to be? (default is 30 minutes)"
    elif not state.preferred_time and not state.selected_slot:
        return "When would you like to schedule this meeting? You can say things like:\n" + \
               "• tomorrow morning\n" + \
               "• next Monday at 2pm\n" + \
               "• June 28 at 10am"
    elif not state.attendees:
        return "Who would you like to invite to this meeting? (Please provide email addresses)"
    return None

def process_user_message(message, state):
    """Process user message and update state"""
    # Extract information from message
    emails = extract_emails(message)
    duration = extract_duration(message)
    time_expr = extract_time_expression(message)
    
    # Update state based on extracted information
    if not state.purpose and not any([emails, duration, time_expr]):
        state.purpose = message
        return {'response': "Great! How long would you like the meeting to be? (default is 30 minutes)"}
    
    if not state.meeting_duration and duration:
        state.meeting_duration = duration
        return {'response': "When would you like to schedule this meeting? You can say things like:\n" + \
                "• tomorrow morning\n" + \
                "• next Monday at 2pm\n" + \
                "• June 28 at 10am"}
    
    if not state.preferred_time and time_expr:
        state.preferred_time = time_expr
        return {'response': "Who would you like to invite to this meeting? (Please provide email addresses)"}
    
    if not state.attendees and emails:
        state.attendees = emails
        return {'response': "Let me check calendar availability..."}
    
    # If we have all required info, proceed with scheduling
    if state.is_complete():
        return {'response': "Let me check calendar availability..."}
    
    # Get next question if we're still missing information
    next_question = get_next_question(state)
    if next_question:
        return {'response': next_question}
    
    # If we get here, something went wrong
    return {'response': "I'm not sure what information you need. Could you please be more specific?"}

def format_confirmation_message(state):
    """Format the meeting confirmation message"""
    if not state.selected_slot:
        return "No slot selected yet."
    
    message = [
        "━━━ Meeting Scheduled! ━━━",
        "",
        f"Purpose: {state.purpose}",
        f"Date: {state.selected_slot.strftime('%A, %B %d')}",
        f"Time: {state.selected_slot.strftime('%I:%M %p')}",
        f"Duration: {state.meeting_duration} minutes",
        f"Attendees: {', '.join(state.attendees)}",
        "",
        "I've sent calendar invites to all attendees."
    ]
    
    return "\n".join(message)

def format_response(conversation_state, response_data=None):
    """Format the response based on conversation state and response data"""
    # If we have response data, use it
    if response_data:
        return response_data.get('response', '')
    
    # If we have all required info, check calendar
    if conversation_state.is_complete():
        return "Let me check calendar availability..."
        
    # Get next piece of information needed
    next_info = conversation_state.get_next_question()
    if next_info:
        return format_info_request(next_info)
    
    # If we get here, something went wrong
    return "I'm not sure what information you need. Could you please be more specific?" 