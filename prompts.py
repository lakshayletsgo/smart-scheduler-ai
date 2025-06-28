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

def format_meeting_details(state):
    """Format current meeting details in a beautiful way"""
    sections = []
    
    if state.purpose:
        sections.append(f"Purpose: {state.purpose}")
    
    if state.meeting_duration:
        sections.append(f"Duration: {state.meeting_duration} minutes")
    
    if state.preferred_time:
        try:
            start_time = dateparser.parse(state.preferred_time['start'], settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now()
            })
            if start_time and isinstance(start_time, datetime):
                time_str = start_time.strftime('%A, %B %d at %I:%M %p')
                sections.append(f"Preferred time: {time_str}")
            else:
                sections.append("Preferred time: Invalid time format")
        except Exception as e:
            logger.error(f"Error formatting preferred time: {e}")
            sections.append("Preferred time: Error in time format")
    
    if state.attendees:
        sections.append(f"Attendees: {', '.join(state.attendees)}")
    
    if sections:
        details = [
            "━━━ Current Meeting Details ━━━",
            "",  # Empty line for spacing
        ]
        for section in sections:
            details.append(f"{section}")
            details.append("")  # Add spacing between sections
        
        return "\n".join(details)
    return ""

def format_missing_info(missing):
    """Format missing information in a beautiful way"""
    if not missing:
        return ""
        
    emoji_map = {
        'purpose': '📝',
        'duration': '⏱️',
        'time': '🗓️',
        'attendees': '👥'
    }
    
    items = [f"{emoji_map[item]} {item.title()}" for item in missing]
    return "\n\n━━━ Still Needed ━━━\n\n" + "\n".join(f"• {item}" for item in items) + "\n"

def format_initial_greeting():
    """Format the initial greeting with proper line breaks"""
    lines = [
        "━━━ Welcome! ━━━",
        "",
        "👋 Hello! I'm your AI scheduling assistant.",
        "",
        "I'll help you schedule your meeting. Let's get started!",
        "",
        "What's the purpose of your meeting?",
        ""
    ]
    return "<br>".join(lines)

def format_info_request(info_type):
    """Format information request messages with proper line breaks"""
    messages = {
        'purpose': [
            "━━━ Meeting Purpose ━━━<br>",
            "<br>",
            "Could you tell me what the meeting is about?<br>",
            "<br>",
            "For example:<br>",
            "• Team sync discussion<br>",
            "• Project planning<br>",
            "• Client presentation<br>",
            "<br>"
        ],
        'duration': [
            "━━━ Meeting Duration ━━━<br>",
            "<br>",
            "How long would you like the meeting to be?<br>",
            "<br>",
            "For example:<br>",
            "• 30 minutes (default)<br>",
            "• 1 hour<br>",
            "• 45 minutes<br>",
            "<br>"
        ],
        'time': [
            "━━━ Meeting Time ━━━<br>",
            "<br>",
            "When would you like to schedule the meeting?<br>",
            "<br>",
            "For example:<br>",
            "• Tomorrow at 2pm<br>",
            "• Next Monday morning<br>",
            "• This Friday afternoon<br>",
            "<br>"
        ],
        'attendees': [
            "━━━ Meeting Attendees ━━━<br>",
            "<br>",
            "Who would you like to invite to the meeting?<br>",
            "<br>",
            "Please provide email addresses, for example:<br>",
            "• john@example.com<br>",
            "• sarah@example.com, mike@example.com<br>",
            "<br>"
        ]
    }
    
    if info_type in messages:
        return "\n".join(messages[info_type])
    return "Could you provide more information?<br>"

def format_available_slots(slots):
    """Format available time slots in a beautiful way"""
    if not slots:
        return "No available slots found."
        
    formatted_slots = ["━━━ Available Slots ━━━\n"]
    for i, slot in enumerate(slots, 1):
        try:
            if isinstance(slot, str):
                parsed_slot = dateparser.parse(slot, settings={
                    'PREFER_DATES_FROM': 'future',
                    'RELATIVE_BASE': datetime.now()
                })
                if parsed_slot and isinstance(parsed_slot, datetime):
                    slot_str = parsed_slot.strftime('%A, %B %d at %I:%M %p')
                else:
                    continue
            elif isinstance(slot, datetime):
                slot_str = slot.strftime('%A, %B %d at %I:%M %p')
            else:
                continue
            formatted_slots.append(f"{i}. {slot_str}")
        except Exception as e:
            logger.error(f"Error formatting slot {slot}: {e}")
            continue
    
    if len(formatted_slots) == 1:  # Only has the header
        return "No valid slots available."
        
    formatted_slots.append("\nPlease choose a slot by entering its number.")
    return "\n".join(formatted_slots)

def format_confirmation(state, slot_time):
    """Format the confirmation message with meeting details"""
    try:
        if isinstance(slot_time, str):
            parsed_time = dateparser.parse(slot_time, settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now()
            })
            if parsed_time and isinstance(parsed_time, datetime):
                slot_time = parsed_time
        
        if not isinstance(slot_time, datetime):
            raise ValueError("Invalid slot time format")
            
        lines = [
            "━━━ Confirm Meeting ━━━",
            "",
            "Here's a summary of your meeting:",
            f"📝 Purpose: {state.purpose}",
            f"⏱️ Duration: {state.meeting_duration} minutes",
            f"📅 Date: {slot_time.strftime('%A, %B %d')}",
            f"🕒 Time: {slot_time.strftime('%I:%M %p')}",
            "👥 Attendees:",
        ]
        
        # Add attendees with bullet points
        for attendee in state.attendees:
            lines.append(f"  • {attendee}")
            
        lines.extend([
            "",
            "Should I go ahead and schedule this meeting? (yes/no)"
        ])
        
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error formatting confirmation: {e}")
        return "I apologize, but there was an error formatting the meeting details. Would you like to try scheduling again?"

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
    """
    Get AI response using Gemini model and update conversation state.
    
    Args:
        user_input: User's message
        conversation_state: ConversationState object to maintain context
    
    Returns:
        dict containing extracted information and response text
    """
    # Extract information from user input
    duration = extract_duration(user_input)
    preferred_time = extract_time_expression(user_input)
    emails = extract_emails(user_input)
    purpose = None
    
    # Extract purpose if not already set
    if not conversation_state.purpose:
        # Common patterns for purpose extraction
        purpose_patterns = [
            r'(?:schedule|set up|arrange|plan|organize|book).*?(?:meeting|call|session)\s+(?:for|about|to discuss|regarding)\s+(.*?)(?:with|at|on|by|\.|\?|$)',
            r'(?:need|want|would like).*?(?:meeting|call|session)\s+(?:for|about|to discuss|regarding)\s+(.*?)(?:with|at|on|by|\.|\?|$)',
            r'(?:purpose|topic|agenda|discuss|about)\s+(?:is|will be|would be)?\s+(.*?)(?:with|at|on|by|\.|\?|$)',
            r'(?:to discuss|discuss about|talk about|regarding)\s+(.*?)(?:with|at|on|by|\.|\?|$)'
        ]
        
        for pattern in purpose_patterns:
            match = re.search(pattern, user_input, re.I)
            if match:
                extracted_purpose = match.group(1).strip()
                # Remove common filler words and clean up the purpose
                filler_words = r'^(the|a|an|some|this|that|these|those|my|our|their)\s+'
                extracted_purpose = re.sub(filler_words, '', extracted_purpose, flags=re.I)
                if extracted_purpose and len(extracted_purpose) > 3:  # Ensure we have a meaningful purpose
                    purpose = extracted_purpose
                    break
    
    # Prepare response data
    response_data = {
        'response': '',  # Will be set below
        'purpose': purpose,
        'duration': duration,
        'time': {'start': preferred_time, 'end': preferred_time + timedelta(days=7)} if preferred_time else None,
        'attendees': emails
    }
    
    # Get current information status
    info_status = get_info_status(conversation_state)
    missing_info = [k for k, v in info_status.items() if v == "❌ Missing"]
    
    # Format current context
    context = f"""Current meeting details:
• Purpose: {conversation_state.purpose or 'Not specified'}
• Duration: {conversation_state.meeting_duration} minutes
• Preferred time: {conversation_state.preferred_time['start'].strftime('%Y-%m-%d %H:%M') if conversation_state.preferred_time else 'Not specified'}
• Attendees: {', '.join(conversation_state.attendees) if conversation_state.attendees else 'Not specified'}
• Current step: {conversation_state.current_step}"""
    
    # Determine appropriate response based on state
    if conversation_state.current_step == 'initial':
        response_data['response'] = format_initial_greeting()
    
    elif conversation_state.current_step == 'gathering_info':
        if missing_info:
            next_info = missing_info[0]
            response_data['response'] = format_info_request(next_info)
            
            # Add current meeting details if we have any
            details = format_meeting_details(conversation_state)
            if details:
                response_data['response'] += "\n" + details
            
            # Add remaining missing info
            missing_info_str = format_missing_info(missing_info[1:])
            if missing_info_str:
                response_data['response'] += missing_info_str
        else:
            response_data['response'] = """🔍 Great! I have all the information needed.

Let me check calendar availability for everyone..."""
    
    elif conversation_state.current_step == 'showing_slots':
        if not conversation_state.available_slots:
            response_data['response'] = """❌ I couldn't find any available slots.

Would you like to:
1. Try different dates
2. Adjust the meeting duration
3. Start over

Just let me know what you prefer!"""
    
    # If no specific response was set, get one from the AI model
    if not response_data['response']:
        # Format messages for Gemini API
        prompt = f"""{SYSTEM_PROMPT}

Required information status:
{chr(10).join(f"• {k}: {v}" for k, v in info_status.items())}

Previous context:
{context}

User: {user_input}"""
        
        # Get AI response
        response = model.generate_content(prompt)
        response_data['response'] = response.text
    
    return response_data

def should_check_calendar(ai_response):
    """Determine if we should check calendar availability based on AI response."""
    check_patterns = [
        r'check.*availability',
        r'find.*time',
        r'available.*slots?',
        r'when.*free',
        r'schedule.*meeting',
        r'check.*calendar',
        r'let me check.*calendar'
    ]
    
    return any(re.search(pattern, ai_response.lower()) for pattern in check_patterns)

def get_next_question(state):
    """Get the next question to ask based on conversation state"""
    next_q = state.get_next_question()
    
    if next_q == 'purpose':
        return "What's the purpose of your meeting?"
    elif next_q == 'duration':
        return "How long would you like the meeting to be (in minutes)?"
    elif next_q == 'time':
        return "When would you like to schedule the meeting?"
    elif next_q == 'attendees':
        return "Who would you like to invite to the meeting? (Please provide email addresses)"
    
    return None

def process_user_message(message, state):
    """Process user message and update state"""
    # Your existing message processing logic here
    # Update to work with Streamlit's session state
    
    response = ""
    
    # If we have all required information
    if state.is_complete():
        if not state.slots_shown:
            # Get available slots from calendar
            creds = st.session_state.credentials
            if creds:
                slots = calendar_utils.find_available_slots(
                    creds,
                    state.attendees,
                    state.preferred_time,
                    state.meeting_duration
                )
                state.available_slots = slots
                response = format_available_slots(slots)
                state.slots_shown = True
        else:
            # Handle slot selection
            try:
                slot_num = int(message.strip())
                if 1 <= slot_num <= len(state.available_slots):
                    state.selected_slot = state.available_slots[slot_num - 1]
                    response = "Great! I'll schedule the meeting. Here's a summary:\n\n"
                    response += format_meeting_details(state)
                else:
                    response = "Please select a valid slot number."
            except ValueError:
                response = "Please select a slot by entering its number."
    
    # If we still need more information
    if not response:
        next_question = get_next_question(state)
        if next_question:
            response = next_question
        else:
            response = "I'm not sure what to ask next. Let me show you the current details:\n\n"
            response += format_meeting_details(state)
    
    return response

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