import re
from datetime import datetime, timedelta
import nltk
from nltk.tokenize import word_tokenize
from nltk.tag import pos_tag
from dateutil import parser
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')
try:
    nltk.data.find('taggers/averaged_perceptron_tagger')
except LookupError:
    nltk.download('averaged_perceptron_tagger')

class MeetingDetails:
    def __init__(self):
        self.date = None
        self.time = None
        self.duration = None
        self.purpose = None
        self.attendees = []

    def to_dict(self):
        return {
            'date': self.date,
            'time': self.time,
            'duration': self.duration,
            'purpose': self.purpose,
            'attendees': self.attendees
        }

    def __str__(self):
        return f"MeetingDetails(purpose={self.purpose}, date={self.date}, time={self.time}, duration={self.duration}, attendees={self.attendees})"

def extract_datetime(text):
    """Extract date and time using simple patterns and dateutil."""
    # Common time patterns
    time_patterns = {
        r'\b(at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?\b': lambda m: f"{m.group(2)}:{m.group(3)} {m.group(4) or 'am'}",
        r'\b(at\s+)?(\d{1,2})\s*(am|pm)\b': lambda m: f"{m.group(2)}:00 {m.group(3)}",
    }
    
    # Common date patterns
    date_patterns = {
        'today': datetime.now(),
        'tomorrow': datetime.now() + timedelta(days=1),
        'next week': datetime.now() + timedelta(days=7),
    }
    
    # Try to find time first
    time_str = None
    for pattern, formatter in time_patterns.items():
        match = re.search(pattern, text.lower())
        if match:
            time_str = formatter(match)
            break
    
    # Try to find date
    date_str = None
    for keyword, date_value in date_patterns.items():
        if keyword in text.lower():
            date_str = date_value.strftime('%Y-%m-%d')
            break
    
    # If explicit patterns don't work, try dateutil parser
    if not (date_str and time_str):
        try:
            parsed = parser.parse(text, fuzzy=True, default=datetime.now())
            if parsed > datetime.now():
                return parsed
        except:
            return None
    else:
        try:
            # Combine date and time if we have both
            if date_str and time_str:
                return parser.parse(f"{date_str} {time_str}")
            elif time_str:
                today = datetime.now().strftime('%Y-%m-%d')
                return parser.parse(f"{today} {time_str}")
        except:
            return None
    
    return None

def extract_duration(text):
    """Extract duration using simple patterns."""
    duration_map = {
        'half hour': 30,
        'hour': 60,
        'one hour': 60,
        'two hours': 120,
        'three hours': 180,
    }
    
    # Check for numeric patterns
    match = re.search(r'(\d+)\s*(hour|hr|min|minute)s?', text.lower())
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        if unit.startswith(('hour', 'hr')):
            return num * 60
        return num
    
    # Check for word patterns
    for phrase, minutes in duration_map.items():
        if phrase in text.lower():
            return minutes
    
    # Default duration if none specified
    return 30

def extract_purpose(text):
    """Extract meeting purpose using POS tagging."""
    # Tokenize and tag parts of speech
    tokens = word_tokenize(text)
    tagged = pos_tag(tokens)
    
    # Look for verb-noun patterns that might indicate purpose
    purpose_indicators = ['discuss', 'review', 'talk about', 'meet about', 'meeting for']
    
    for indicator in purpose_indicators:
        if indicator in text.lower():
            start_idx = text.lower().find(indicator) + len(indicator)
            end_idx = text.find('.', start_idx)
            if end_idx == -1:
                end_idx = len(text)
            purpose = text[start_idx:end_idx].strip()
            if purpose:
                return purpose
    
    # If no clear indicator, look for noun phrases after "meeting" or "call"
    words = text.split()
    for i, word in enumerate(words):
        if word.lower() in ['meeting', 'call']:
            if i + 1 < len(words):
                return ' '.join(words[i+1:i+6])  # Take next 5 words as purpose
    
    return None

def extract_attendees(text):
    """Extract email addresses from text."""
    email_pattern = r'\b[\w\.-]+@[\w\.-]+\.\w+\b'
    return re.findall(email_pattern, text)

def extract_meeting_details(text):
    """Extract all meeting details using simplified patterns."""
    details = MeetingDetails()
    logger.debug(f"Processing text: {text}")
    
    # Extract date and time
    parsed_datetime = extract_datetime(text)
    if parsed_datetime:
        details.date = parsed_datetime.strftime('%Y-%m-%d')
        details.time = parsed_datetime.strftime('%H:%M')
        logger.debug(f"Extracted date/time: {details.date} {details.time}")
    
    # Extract duration
    duration = extract_duration(text)
    if duration:
        details.duration = duration
        logger.debug(f"Extracted duration: {duration} minutes")
    
    # Extract purpose
    purpose = extract_purpose(text)
    if purpose:
        details.purpose = purpose
        logger.debug(f"Extracted purpose: {purpose}")
    
    # Extract attendees
    attendees = extract_attendees(text)
    if attendees:
        details.attendees = attendees
        logger.debug(f"Extracted attendees: {attendees}")
    
    logger.debug(f"Final extracted details: {details}")
    return details 