from google.generativeai import GenerativeModel
import google.generativeai as genai
import re
from datetime import datetime, timedelta
import dateparser
import os

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

def extract_time_expression(text):
    """Parse natural language time expressions into structured datetime objects."""
    # Try to parse the entire expression first
    parsed_time = dateparser.parse(text, settings={'PREFER_DATES_FROM': 'future'})
    if parsed_time:
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
            return time_func()
    
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

def get_ai_response(user_input, conversation_state):
    """
    Get AI response using Gemini model and update conversation state.
    
    Args:
        user_input: User's message
        conversation_state: ConversationState object to maintain context
    
    Returns:
        AI response text
    """
    # Extract information from user input
    duration = extract_duration(user_input)
    if duration and not conversation_state.meeting_duration:
        conversation_state.meeting_duration = duration
    
    preferred_time = extract_time_expression(user_input)
    if preferred_time and not conversation_state.preferred_time:
        conversation_state.preferred_time = {
            'start': preferred_time,
            'end': preferred_time + timedelta(days=7)  # Look for slots within a week
        }
    
    emails = extract_emails(user_input)
    if emails:
        # Remove duplicates while preserving order
        conversation_state.attendees = list(dict.fromkeys(conversation_state.attendees + emails))
    
    # Extract purpose from user input if not already set
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
                    conversation_state.purpose = extracted_purpose
                    break
    
    # Get current information status
    info_status = get_info_status(conversation_state)
    
    # Format current context
    context = f"""Current meeting details:
• Purpose: {conversation_state.purpose or 'Not specified'}
• Duration: {conversation_state.meeting_duration} minutes
• Preferred time: {conversation_state.preferred_time['start'].strftime('%Y-%m-%d %H:%M') if conversation_state.preferred_time else 'Not specified'}
• Attendees: {', '.join(conversation_state.attendees) if conversation_state.attendees else 'Not specified'}"""
    
    # Format messages for Gemini API
    prompt = f"""{SYSTEM_PROMPT}

Required information status:
{chr(10).join(f"• {k}: {v}" for k, v in info_status.items())}

Previous context:
{context}

User: {user_input}"""
    
    # Get AI response
    response = model.generate_content(prompt)
    ai_response = response.text
    
    # Update conversation state based on AI response if we still don't have a purpose
    if not conversation_state.purpose and "purpose" in ai_response.lower():
        for pattern in purpose_patterns:
            match = re.search(pattern, ai_response, re.I)
            if match:
                extracted_purpose = match.group(1).strip()
                filler_words = r'^(the|a|an|some|this|that|these|those|my|our|their)\s+'
                extracted_purpose = re.sub(filler_words, '', extracted_purpose, flags=re.I)
                if extracted_purpose and len(extracted_purpose) > 3:
                    conversation_state.purpose = extracted_purpose
                    break
    
    # Check if we have all required information
    all_info_ready = all(value == "✅ Set" for value in info_status.values())
    
    # If we have all information and no slots shown yet, trigger calendar check
    if all_info_ready and not conversation_state.available_slots:
        return "Great! Let me check the calendar for available slots."
    
    return ai_response

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

def format_available_slots(slots):
    """Format available time slots into a human-readable response."""
    if not slots:
        return "I couldn't find any available time slots matching your criteria. Would you like to try different times?"
    
    formatted_slots = []
    for slot in slots[:5]:  # Limit to 5 suggestions
        formatted_slots.append(slot.strftime("%A, %B %d at %I:%M %p"))
    
    response = "📅 Here are the best available time slots I found:\n\n"
    for i, slot in enumerate(formatted_slots, 1):
        response += f"🕒 Option {i}: {slot}\n"
    
    response += "\n✨ When you choose a slot:\n"
    response += "1. The meeting will be automatically added to your Google Calendar\n"
    response += "2. All attendees will receive an email invitation\n"
    response += "3. You can manage the meeting directly from your Google Calendar\n\n"
    response += "Which time slot would you prefer? (Just reply with the option number 1-5)"
    return response 