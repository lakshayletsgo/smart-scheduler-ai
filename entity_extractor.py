import spacy
import re
from datetime import datetime
from dateutil import parser
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load spaCy model
try:
    nlp = spacy.load('en_core_web_sm')
except OSError:
    import subprocess
    subprocess.run(['python', '-m', 'spacy', 'download', 'en_core_web_sm'])
    nlp = spacy.load('en_core_web_sm')

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
    """Extract date and time using spaCy and dateparser."""
    doc = nlp(text)
    
    # Collect all date/time related entities
    datetime_texts = []
    for ent in doc.ents:
        if ent.label_ in ['DATE', 'TIME']:
            datetime_texts.append(ent.text)
    
    # Also look for time patterns that spaCy might miss
    time_patterns = [
        r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b',
        r'\b(?:at|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b',
        r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:today|tomorrow|next|this)\b'
    ]
    
    for pattern in time_patterns:
        matches = re.finditer(pattern, text, re.I)
        for match in matches:
            if match.group() not in datetime_texts:
                datetime_texts.append(match.group())
    
    # Try to parse the combined date/time text
    if datetime_texts:
        try:
            parsed_date = parser.parse(
                ' '.join(datetime_texts),
                settings={
                    'PREFER_DATES_FROM': 'future',
                    'RELATIVE_BASE': datetime.now(),
                    'PREFER_DAY_OF_MONTH': 'first'
                }
            )
            if parsed_date > datetime.now():
                return parsed_date
        except Exception as e:
            logger.error(f"Error parsing datetime from entities: {e}")
    
    # Try parsing the full text as fallback
    try:
        parsed_date = parser.parse(
            text,
            settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now(),
                'PREFER_DAY_OF_MONTH': 'first'
            }
        )
        if parsed_date > datetime.now():
            return parsed_date
    except Exception as e:
        logger.error(f"Error parsing datetime from full text: {e}")
    
    return None

def extract_duration(text):
    """Extract duration using regex patterns."""
    # Check for numeric patterns first
    duration_patterns = [
        (r'(\d+)\s*(hour|hr|min|minute|minutes?|hrs?)', 
         lambda x: int(float(x[0]) * (60 if x[1].startswith(('hour', 'hr')) else 1))),
        (r'(half|one|two|three|four|five)\s*(hour|hr)', 
         lambda x: {'half': 30, 'one': 60, 'two': 120, 'three': 180, 'four': 240, 'five': 300}[x[0]])
    ]
    
    for pattern, converter in duration_patterns:
        match = re.search(pattern, text.lower())
        if match:
            try:
                return converter(match.groups())
            except (ValueError, KeyError) as e:
                logger.error(f"Error converting duration: {e}")
    
    return None

def extract_purpose(text):
    """Extract meeting purpose using spaCy and patterns."""
    doc = nlp(text)
    
    # Try verb-based patterns first
    purpose_verbs = ['discuss', 'talk', 'review', 'plan', 'meet', 'schedule']
    for token in doc:
        if token.lemma_.lower() in purpose_verbs:
            for child in token.children:
                if child.dep_ in ['dobj', 'pobj', 'attr']:
                    purpose = ' '.join(t.text for t in child.subtree)
                    if len(purpose) > 3:
                        return purpose.strip()
    
    # Try regex patterns
    purpose_patterns = [
        r'(?:meeting|call|discussion) (?:about|for|to|regarding) (.*?)(?=(?:with|at|on|by|\.|$))',
        r'(?:discuss|talk about|review) (.*?)(?=(?:with|at|on|by|\.|$))',
        r'purpose is (.*?)(?=(?:with|at|on|by|\.|$))'
    ]
    
    for pattern in purpose_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            purpose = match.group(1).strip()
            if len(purpose) > 3:
                return purpose
    
    # Try first sentence that doesn't contain date/time entities
    for sent in doc.sents:
        if not any(ent.label_ in ['DATE', 'TIME'] for ent in sent.ents):
            purpose = sent.text.strip()
            if len(purpose) > 3:
                return purpose
    
    return None

def extract_attendees(text):
    """Extract email addresses from text."""
    email_pattern = r'\b[\w\.-]+@[\w\.-]+\.\w+\b'
    return re.findall(email_pattern, text)

def extract_meeting_details(text):
    """Extract all meeting details using improved entity recognition."""
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