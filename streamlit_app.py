import streamlit as st
import os
from google.cloud import aiplatform
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from datetime import datetime, timedelta
from googleapiclient.discovery import build
import calendar_utils
import prompts
from pathlib import Path
from dotenv import load_dotenv
import re
from dateutil import parser
import json
import logging
from voice_bot import VoiceBot
import asyncio
import sys

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Set page config first
st.set_page_config(
    page_title="AI Meeting Scheduler",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Initialize Google Cloud AI Platform if project ID is available
if os.getenv('GOOGLE_CLOUD_PROJECT'):
    try:
        aiplatform.init(project=os.getenv('GOOGLE_CLOUD_PROJECT'))
    except Exception as e:
        logger.error(f"Error initializing AI Platform: {str(e)}")

# Google OAuth2 Configuration
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), 'client_secrets.json')
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.events']

# Simple text tokenizer as fallback
def simple_tokenize(text):
    """Simple tokenizer as fallback if NLTK is not available"""
    # Split on common punctuation and whitespace
    tokens = re.findall(r'\b\w+\b|[.,!?;]', text)
    return tokens

def simple_sentence_tokenize(text):
    """Simple sentence tokenizer as fallback if NLTK is not available"""
    # Split on common sentence endings
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return sentences

# Try to use NLTK, fall back to simple tokenizers if not available
try:
    import nltk
    # Set NLTK data path to a writable location
    nltk.data.path.append(os.path.join(os.path.expanduser("~"), "nltk_data"))
    
    try:
        # Try to load NLTK data
        nltk.data.find('tokenizers/punkt')
        nltk.data.find('taggers/averaged_perceptron_tagger')
        nltk.data.find('chunkers/maxent_ne_chunker')
        nltk.data.find('corpora/words')
        
        # If successful, use NLTK functions
        word_tokenize = nltk.word_tokenize
        sent_tokenize = nltk.sent_tokenize
        pos_tag = nltk.pos_tag
        ne_chunk = nltk.ne_chunk
        
    except LookupError:
        try:
            # Try to download NLTK data
            nltk.download('punkt', quiet=True)
            nltk.download('averaged_perceptron_tagger', quiet=True)
            nltk.download('maxent_ne_chunker', quiet=True)
            nltk.download('words', quiet=True)
            
            # If download successful, use NLTK functions
            word_tokenize = nltk.word_tokenize
            sent_tokenize = nltk.sent_tokenize
            pos_tag = nltk.pos_tag
            ne_chunk = nltk.ne_chunk
            
        except Exception as e:
            logger.warning(f"Could not download NLTK data: {e}. Using simple tokenizers.")
            # Fall back to simple tokenizers
            word_tokenize = simple_tokenize
            sent_tokenize = simple_sentence_tokenize
            def simple_pos_tag(tokens):
                return [(token, 'NN') for token in tokens]  # Treat all words as nouns
            pos_tag = simple_pos_tag
            ne_chunk = lambda x: x  # No-op for named entity chunking
            
except ImportError:
    logger.warning("NLTK not available. Using simple tokenizers.")
    # Fall back to simple tokenizers
    word_tokenize = simple_tokenize
    sent_tokenize = simple_sentence_tokenize
    def simple_pos_tag(tokens):
        return [(token, 'NN') for token in tokens]  # Treat all words as nouns
    pos_tag = simple_pos_tag
    ne_chunk = lambda x: x  # No-op for named entity chunking

# Get the deployment URL from Streamlit's environment or use localhost as fallback
def get_oauth_redirect_uri():
    """Get the OAuth redirect URI that matches the client_secrets.json configuration"""
    # Use the production URL if available, otherwise fallback to localhost
    if os.getenv('STREAMLIT_SERVER_URL'):
        return "https://lakshayletsgo-smart-scheduler-ai-streamlit-app-xrbctg.streamlit.app/oauth2callback"
    return "http://localhost:5000/oauth2callback"

class ConversationState:
    def __init__(self):
        self.purpose = None
        self.meeting_duration = 30  # Default duration in minutes
        self.preferred_time = None  # Will be a dict with 'start' and 'end' datetime objects
        self.attendees = set()
        self.answered_questions = set()
        self.current_step = 'initial'
        self.slots_shown = False
        self.available_slots = []
        self.last_question_asked = None
        self.selected_slot = None

    def reset(self):
        """Reset the conversation state"""
        self.__init__()

    def set_preferred_time(self, start_time, duration_minutes=None):
        """Set the preferred time with proper validation"""
        if not isinstance(start_time, datetime):
            try:
                start_time = parser.parse(str(start_time))
            except (ValueError, TypeError):
                return False

        # Ensure the time is in the future
        if start_time <= datetime.now():
            return False

        # Use provided duration or default
        duration = duration_minutes or self.meeting_duration or 30
        
        self.preferred_time = {
            'start': start_time,
            'end': start_time + timedelta(minutes=duration)
        }
        return True

    def is_complete(self):
        """Check if all required information has been gathered."""
        return (
            self.purpose is not None
            and self.meeting_duration is not None
            and (self.preferred_time is not None or self.selected_slot is not None)
            and len(self.attendees) > 0
        )

    def get_missing_info(self):
        """Get a list of all missing pieces of information."""
        missing = []
        if self.purpose is None:
            missing.append('purpose')
        if self.meeting_duration is None:
            missing.append('duration')
        if self.preferred_time is None and self.selected_slot is None:
            missing.append('time')
        if not self.attendees:
            missing.append('attendees')
        return missing

    def get_next_question(self):
        """Get the next piece of information we need to ask for."""
        missing = self.get_missing_info()
        return missing[0] if missing else None

    def to_dict(self):
        """Convert state to JSON-serializable dictionary"""
        return {
            'purpose': self.purpose,
            'meeting_duration': self.meeting_duration,
            'preferred_time': {
                'start': self.preferred_time['start'].isoformat() if self.preferred_time else None,
                'end': self.preferred_time['end'].isoformat() if self.preferred_time else None
            } if self.preferred_time else None,
            'attendees': list(self.attendees),
            'answered_questions': list(self.answered_questions),
            'current_step': self.current_step,
            'slots_shown': self.slots_shown,
            'available_slots': [slot.isoformat() if isinstance(slot, datetime) else slot for slot in self.available_slots],
            'last_question_asked': self.last_question_asked,
            'selected_slot': self.selected_slot.isoformat() if self.selected_slot else None
        }

    def from_dict(self, data):
        """Update state from a dictionary"""
        self.purpose = data.get('purpose')
        self.meeting_duration = data.get('meeting_duration', 30)
        
        # Handle preferred_time conversion
        preferred_time = data.get('preferred_time')
        if preferred_time and preferred_time.get('start'):
            try:
                start = parser.parse(preferred_time['start'])
                end = parser.parse(preferred_time['end'])
                self.preferred_time = {'start': start, 'end': end}
            except (ValueError, TypeError):
                self.preferred_time = None
        
        # Handle available_slots conversion
        available_slots = data.get('available_slots', [])
        self.available_slots = []
        for slot in available_slots:
            try:
                if isinstance(slot, str):
                    self.available_slots.append(parser.parse(slot))
                else:
                    self.available_slots.append(slot)
            except (ValueError, TypeError):
                continue
        
        # Handle selected_slot conversion
        selected_slot = data.get('selected_slot')
        if selected_slot:
            try:
                self.selected_slot = parser.parse(selected_slot)
            except (ValueError, TypeError):
                self.selected_slot = None
        
        self.attendees = set(data.get('attendees', []))
        self.answered_questions = set(data.get('answered_questions', []))
        self.current_step = data.get('current_step', 'initial')
        self.slots_shown = data.get('slots_shown', False)
        self.last_question_asked = data.get('last_question_asked')

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

def extract_meeting_details(text):
    """Extract meeting details using improved regex patterns and NLTK."""
    details = MeetingDetails()
    logger.debug(f"Extracting details from: {text}")

    # Tokenize and get named entities
    tokens = word_tokenize(text)
    pos_tags = pos_tag(tokens)
    named_entities = ne_chunk(pos_tags)

    # Extract duration with improved patterns
    duration_match = re.search(r'(\d+)\s*(hour|hr|min|minutes?|hrs?)', text.lower())
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)
        if unit.startswith(('hour', 'hr')):
            amount *= 60
        details.duration = amount
        logger.debug(f"Extracted duration: {amount} minutes")

    # Extract date/time using improved patterns
    try:
        # Common time expressions with specific times
        time_patterns = {
            r'tomorrow\s+morning': lambda: (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0),
            r'tomorrow\s+afternoon': lambda: (datetime.now() + timedelta(days=1)).replace(hour=14, minute=0),
            r'tomorrow\s+evening': lambda: (datetime.now() + timedelta(days=1)).replace(hour=17, minute=0),
            r'tomorrow\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?': lambda m: parse_time_with_meridiem(m, tomorrow=True),
            r'next\s+monday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?': lambda m: parse_time_with_meridiem(m, next_weekday=0),
            r'next\s+tuesday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?': lambda m: parse_time_with_meridiem(m, next_weekday=1),
            r'next\s+wednesday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?': lambda m: parse_time_with_meridiem(m, next_weekday=2),
            r'next\s+thursday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?': lambda m: parse_time_with_meridiem(m, next_weekday=3),
            r'next\s+friday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?': lambda m: parse_time_with_meridiem(m, next_weekday=4),
            r'next\s+monday': lambda: get_next_weekday(0),
            r'next\s+tuesday': lambda: get_next_weekday(1),
            r'next\s+wednesday': lambda: get_next_weekday(2),
            r'next\s+thursday': lambda: get_next_weekday(3),
            r'next\s+friday': lambda: get_next_weekday(4),
            r'tomorrow': lambda: datetime.now() + timedelta(days=1)
        }

        def parse_time_with_meridiem(match, tomorrow=False, next_weekday=None):
            hour = int(match.group(1))
            minute = int(match.group(2)) if match.group(2) else 0
            meridiem = match.group(3)

            # Adjust hour for PM
            if meridiem and meridiem.lower() == 'pm' and hour < 12:
                hour += 12
            elif not meridiem and hour < 9:  # Default to AM for ambiguous times before 9
                hour += 12

            base_date = datetime.now()
            if tomorrow:
                base_date += timedelta(days=1)
            elif next_weekday is not None:
                base_date = get_next_weekday(next_weekday)

            return base_date.replace(hour=hour, minute=minute)

        def get_next_weekday(weekday):
            today = datetime.now()
            days_ahead = weekday - today.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            next_day = today + timedelta(days=days_ahead)
            return next_day.replace(hour=9, minute=0)  # Default to 9 AM

        # First try to match common time expressions
        parsed_date = None
        for pattern, time_func in time_patterns.items():
            match = re.search(pattern, text.lower())
            if match:
                if callable(time_func):
                    if len(match.groups()) > 0:
                        parsed_date = time_func(match)
                    else:
                        parsed_date = time_func()
                else:
                    parsed_date = time_func
                break

        # If no common expression found, try to find explicit date/time
        if not parsed_date:
            # Look for specific date formats (e.g., "28 June 2025")
            date_match = re.search(r'(\d{1,2})\s*(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*(\d{4})?', text)
            if date_match:
                date_str = ' '.join(part for part in date_match.groups() if part)
                parsed_date = parser.parse(date_str, fuzzy=True)

            # Look for specific time (e.g., "at 9am", "at 14:30")
            time_match = re.search(r'(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?', text)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2)) if time_match.group(2) else 0
                meridiem = time_match.group(3)

                # Adjust hour for PM
                if meridiem and meridiem.lower() == 'pm' and hour < 12:
                    hour += 12
                elif not meridiem and hour < 9:  # Default to AM for ambiguous times before 9
                    hour += 12

                if parsed_date:
                    parsed_date = parsed_date.replace(hour=hour, minute=minute)
                else:
                    # If we only have time, use tomorrow as default date
                    parsed_date = datetime.now().replace(hour=hour, minute=minute)
                    if parsed_date <= datetime.now():
                        parsed_date += timedelta(days=1)

        # If still no parsed_date, try dateparser as fallback
        if not parsed_date:
            parsed_date = parser.parse(text, settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now(),
                'PREFER_DAY_OF_MONTH': 'first',
                'DATE_ORDER': 'DMY'
            })

        if parsed_date:
            # Ensure we're not scheduling in the past
            if parsed_date <= datetime.now():
                if parsed_date.date() == datetime.now().date():
                    # If it's today but time is in past, try next available hour
                    parsed_date = datetime.now().replace(
                        minute=0, second=0, microsecond=0
                    ) + timedelta(hours=1)
                else:
                    # If date is in past, move it to tomorrow same time
                    parsed_date = parsed_date + timedelta(days=1)

            details.date = parsed_date.strftime('%Y-%m-%d')
            details.time = parsed_date.strftime('%H:%M')
            logger.debug(f"Extracted date/time: {details.date} {details.time}")

    except Exception as e:
        logger.error(f"Error parsing date/time: {e}")

    # Extract email addresses
    email_pattern = r'\b[\w\.-]+@[\w\.-]+\.\w+\b'
    emails = re.findall(email_pattern, text)
    if emails:
        details.attendees = emails
        logger.debug(f"Extracted attendees: {emails}")

    # Extract purpose with improved patterns
    sentences = sent_tokenize(text)
    purpose_patterns = [
        r'(?:schedule|set up|arrange|plan|organize|book).*?(?:meeting|call|session)\s+(?:for|about|to discuss|regarding)\s+(.*?)(?=\s+(?:with|at|on|by|\.|\?|$))',
        r'(?:need|want|would like).*?(?:meeting|call|session)\s+(?:for|about|to discuss|regarding)\s+(.*?)(?=\s+(?:with|at|on|by|\.|\?|$))',
        r'(?:purpose|topic|agenda|discuss|about)\s+(?:is|will be|would be)?\s+(.*?)(?=\s+(?:with|at|on|by|\.|\?|$))',
        r'(?:to discuss|discuss about|talk about|regarding)\s+(.*?)(?=\s+(?:with|at|on|by|\.|\?|$))'
    ]

    for sentence in sentences:
        for pattern in purpose_patterns:
            match = re.search(pattern, sentence, re.I)
            if match:
                purpose = match.group(1).strip()
                # Clean up the purpose text
                purpose = re.sub(r'^(the|a|an|some|this|that|these|those|my|our|their)\s+', '', purpose, flags=re.I)
                if purpose and len(purpose) > 3:
                    details.purpose = purpose
                    logger.debug(f"Extracted purpose: {purpose}")
                    break
        if details.purpose:
            break

    # If no purpose found with patterns, try first sentence without time/date/email
    if not details.purpose:
        for sentence in sentences:
            # Skip if sentence contains time/date/email indicators
            if not re.search(r'\b(?:today|tomorrow|next|at|on|pm|am|:\d{2}|\d{1,2}(?::\d{2})?|@)\b', sentence.lower()):
                # Extract meaningful parts using POS tags
                sentence_tokens = word_tokenize(sentence)
                sentence_pos = pos_tag(sentence_tokens)
                meaningful_parts = []
                for token, pos in sentence_pos:
                    if pos.startswith(('NN', 'VB', 'JJ')) and token.lower() not in ['schedule', 'meeting', 'call']:
                        meaningful_parts.append(token)
                if meaningful_parts:
                    details.purpose = ' '.join(meaningful_parts)
                    logger.debug(f"Extracted purpose from sentence: {details.purpose}")
                    break

    logger.debug(f"Final extracted details: {details}")
    return details

# Initialize session state
if 'conversation_state' not in st.session_state:
    st.session_state.conversation_state = ConversationState()
    st.session_state.initialized = False
elif isinstance(st.session_state.conversation_state, dict):
    # Convert dictionary state back to ConversationState object
    state = ConversationState()
    state.from_dict(st.session_state.conversation_state)
    st.session_state.conversation_state = state

if 'credentials' not in st.session_state:
    st.session_state.credentials = None
if 'messages' not in st.session_state:
    st.session_state.messages = []
    # Add initial welcome message only once
    if not st.session_state.initialized:
        initial_greeting = "━━━ Welcome! ━━━\n\n👋 Hello! I'm your AI scheduling assistant.\n\nI'll help you schedule your meeting. Let's get started!\n\nWhat's the purpose of your meeting?"
        st.session_state.messages.append({"role": "assistant", "content": initial_greeting})
        st.session_state.initialized = True
if 'oauth_state' not in st.session_state:
    st.session_state.oauth_state = None
if 'voice_bot' not in st.session_state:
    st.session_state.voice_bot = None
if 'call_active' not in st.session_state:
    st.session_state.call_active = False

# Store the OAuth state in a more persistent way
if 'state' in st.query_params:
    st.session_state.oauth_state = st.query_params['state']

def initialize_conversation_state():
    """Initialize or reset conversation state"""
    st.session_state.conversation_state = ConversationState()

def get_calendar_credentials():
    """Get valid credentials for Google Calendar API."""
    creds = st.session_state.credentials
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                st.session_state.credentials = creds
            except Exception as e:
                logger.error(f"Error refreshing credentials: {e}")
                return None
        else:
            return None
    
    return creds

def authorize_google_calendar():
    """Start Google Calendar authorization flow"""
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=get_oauth_redirect_uri()
    )
    
    # Generate a secure state parameter
    state = os.urandom(16).hex()
    st.session_state.oauth_state = state
    
    authorization_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        state=state,
        prompt='consent'  # Always show consent screen to avoid token expiry issues
    )
    
    st.markdown(f"[Authorize Google Calendar]({authorization_url})")

def handle_oauth_callback():
    """Handle the OAuth2 callback from Google"""
    try:
        # Get the authorization code and state from URL parameters
        code = st.query_params.get('code', None)
        received_state = st.query_params.get('state', None)
        stored_state = st.session_state.oauth_state
        
        # Debug logging
        logger.debug(f"Received state: {received_state}")
        logger.debug(f"Stored state: {stored_state}")
        
        # Verify state parameter to prevent CSRF
        if not received_state or not stored_state or received_state != stored_state:
            st.error("Invalid state parameter. Please try authenticating again.")
            st.session_state.oauth_state = None
            st.query_params.clear()
            return
        
        if code:
            # Create flow instance to handle the callback
            flow = Flow.from_client_secrets_file(
                CLIENT_SECRETS_FILE,
                scopes=SCOPES,
                redirect_uri=get_oauth_redirect_uri(),
                state=received_state
            )
            
            try:
                # Exchange authorization code for credentials
                flow.fetch_token(code=code)
                credentials = flow.credentials
                
                # Store credentials in session state
                st.session_state.credentials = credentials
                
                # Clear OAuth state after successful authentication
                st.session_state.oauth_state = None
                
                st.success("Successfully authenticated with Google Calendar!")
                
                # Clear the query parameters and refresh
                st.query_params.clear()
                st.rerun()
                
            except Exception as token_error:
                logger.error(f"Token exchange error: {str(token_error)}")
                st.error("Failed to exchange authorization code for tokens. Please try again.")
                st.session_state.oauth_state = None
                st.query_params.clear()
                
        else:
            st.error("No authorization code received.")
            st.session_state.oauth_state = None
            st.query_params.clear()
            
    except Exception as e:
        st.error(f"Error during authentication: {str(e)}")
        logger.error(f"Authentication error: {str(e)}")
        st.session_state.oauth_state = None
        st.query_params.clear()

def show_chat_interface():
    """Display the chat interface"""
    # Add custom CSS for chat interface
    st.markdown("""
    <style>
    .chat-header {
        text-align: center;
        margin-bottom: 2rem;
    }
    .chat-title {
        color: #1f2937;
        font-size: 1.8rem;
        font-weight: 600;
        margin-bottom: 0.5rem;
    }
    .chat-subtitle {
        color: #4b5563;
        font-size: 1.1rem;
    }
    .stExpander {
        border: none !important;
        box-shadow: none !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # Chat header
    st.markdown("""
    <div class="chat-header">
        <h1 class="chat-title">AI Meeting Scheduler</h1>
        <p class="chat-subtitle">Just chat naturally to schedule your meetings</p>
    </div>
    """, unsafe_allow_html=True)
    
    # Add debug expander (hidden by default)
    with st.expander("Debug Info", expanded=False):
        state = st.session_state.conversation_state
        st.write("Current State:")
        st.write(f"- Purpose: {state.purpose}")
        st.write(f"- Duration: {state.meeting_duration} minutes")
        
        # Display time information
        if state.preferred_time and state.preferred_time['start']:
            st.write(f"- Preferred Time: {state.preferred_time['start'].strftime('%A, %B %d at %I:%M %p')}")
        elif state.selected_slot:
            st.write(f"- Selected Time: {state.selected_slot.strftime('%A, %B %d at %I:%M %p')}")
        else:
            st.write("- Time: Not set")
            
        st.write(f"- Attendees: {', '.join(state.attendees) if state.attendees else 'None'}")
        st.write(f"- Current Step: {state.current_step}")
        st.write(f"- Answered Questions: {', '.join(state.answered_questions)}")
        st.write(f"- Slots Shown: {state.slots_shown}")
        
        if state.available_slots:
            st.write("- Available Slots:")
            for i, slot in enumerate(state.available_slots, 1):
                if isinstance(slot, datetime):
                    st.write(f"  {i}. {slot.strftime('%A, %B %d at %I:%M %p')}")
                else:
                    st.write(f"  {i}. {slot}")
    
    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    
    # Chat input
    if prompt := st.chat_input("Type your message here..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})
        
        # Process user input and get AI response
        response = process_message(prompt)
        
        # Add AI response to chat history
        st.session_state.messages.append({"role": "assistant", "content": response})
        
        # Rerun to update the UI
        st.rerun()

def process_message(message):
    """Process user message and return AI response"""
    # Get the current state from session state
    state = st.session_state.conversation_state
    logger.debug(f"Processing message: {message}")
    logger.debug(f"Current state: {state.to_dict()}")
    
    # Handle reset command
    if message.lower() in ['reset', 'start over', 'restart']:
        state.reset()
        st.session_state.messages = []
        st.session_state.initialized = False
        initial_greeting = "━━━ Welcome! ━━━\n\n👋 Hello! I'm your AI scheduling assistant.\n\nI'll help you schedule your meeting. Let's get started!\n\nWhat's the purpose of your meeting?"
        return initial_greeting
    
    # Extract meeting details from user message
    details = extract_meeting_details(message)
    response_parts = []
    state_updated = False
    
    # Update state with extracted information
    if details.purpose and not state.purpose:
        state.purpose = details.purpose
        state.answered_questions.add('purpose')
        response_parts.append(f"I understand the purpose is: {details.purpose}")
        state_updated = True
        logger.debug(f"Updated purpose: {details.purpose}")
        
    if details.duration and not state.meeting_duration:
        state.meeting_duration = details.duration
        state.answered_questions.add('duration')
        response_parts.append(f"Meeting duration set to {details.duration} minutes")
        state_updated = True
        logger.debug(f"Updated duration: {details.duration}")
    
    # Handle time extraction
    if details.date and details.time:
        try:
            # Combine date and time
            date_str = f"{details.date} {details.time}"
            if state.set_preferred_time(date_str):
                state.answered_questions.add('time')
                formatted_time = state.preferred_time['start'].strftime('%A, %B %d at %I:%M %p')
                response_parts.append(f"Meeting time set to: {formatted_time}")
                state_updated = True
                logger.debug(f"Updated time: {state.preferred_time}")
            else:
                # If the time is invalid (past), ask for a future time
                response_parts.append("Please provide a future date and time for the meeting.")
                if 'time' in state.answered_questions:
                    state.answered_questions.remove('time')
                state.preferred_time = None
        except ValueError as e:
            logger.error(f"Error parsing date/time: {e}")
            response_parts.append("I couldn't understand the date and time. Please provide them in a clearer format.")
            if 'time' in state.answered_questions:
                state.answered_questions.remove('time')
            state.preferred_time = None
    
    if details.attendees:
        new_attendees = [email for email in details.attendees if email not in state.attendees]
        if new_attendees:
            state.attendees.update(new_attendees)
            state.answered_questions.add('attendees')
            attendee_list = "\n".join([f"• {attendee}" for attendee in new_attendees])
            response_parts.append(f"Added attendees:\n{attendee_list}")
            state_updated = True
            logger.debug(f"Updated attendees: {new_attendees}")
        
    # Log current state for debugging
    logger.debug(f"Updated state: {state.to_dict()}")
    
    # If we have all required information and haven't shown slots yet
    if state.is_complete() and not state.slots_shown:
        # Get calendar credentials
        creds = st.session_state.credentials
        if creds:
            try:
            # Get available slots from calendar
                available_slots = calendar_utils.find_available_slots(
                creds,
                start_time=state.preferred_time['start'] if state.preferred_time else None,
                    duration_minutes=state.meeting_duration,
                    attendees=list(state.attendees)
                )
                
                if available_slots:
                    # Automatically select the first available slot
                    selected_slot = available_slots[0]
                    state.selected_slot = selected_slot
                    state.current_step = 'confirming'
                    
                    # Format confirmation message
                    return (f"I found an available slot for your meeting: {selected_slot.strftime('%A, %B %d at %I:%M %p')}.\n\n"
                           f"Here's a summary of your meeting:\n"
                           f"📝 Purpose: {state.purpose}\n"
                           f"⏱️ Duration: {state.meeting_duration} minutes\n"
                           f"👥 Attendees: {', '.join(state.attendees)}\n\n"
                           "Should I go ahead and schedule this meeting? (yes/no)")
                else:
                    return "I couldn't find any available slots in the next week. Would you like to try a different time?"
            except Exception as e:
                logger.error(f"Error finding available slots: {e}")
                return "I apologize, but there was an error checking calendar availability. Would you like to try again?"
        else:
            return "Please authorize access to Google Calendar first."
    
    # If we're in confirming state, handle confirmation
    if state.current_step == 'confirming':
        if message.lower() in ['yes', 'y', 'sure', 'ok', 'okay']:
            # Get calendar credentials
            creds = st.session_state.credentials
            if creds:
                try:
                    # Create the calendar event
                    success = calendar_utils.create_calendar_event(
                        creds,
                        summary=state.purpose,
                        start_time=state.selected_slot,
                        attendees=list(state.attendees),
                        duration_minutes=state.meeting_duration
                    )
                    
                    if success:
                        # Reset state for next meeting
                        response = (f"✅ Perfect! I've scheduled the meeting:\n\n"
                                  f"📝 Purpose: {state.purpose}\n"
                                  f"📅 Time: {state.selected_slot.strftime('%A, %B %d at %I:%M %p')}\n"
                                  f"⏱️ Duration: {state.meeting_duration} minutes\n"
                                  f"👥 Attendees:\n" + "\n".join([f"• {attendee}" for attendee in state.attendees]) +
                                  "\n\nI've sent calendar invites to all attendees.")
                        state.reset()
                        return response
                    else:
                        return "I apologize, but there was an error scheduling the meeting. Would you like to try a different time?"
                except Exception as e:
                    logger.error(f"Error creating calendar event: {e}")
                    return "I apologize, but there was an error scheduling the meeting. Would you like to try a different time?"
            else:
                return "Please authorize access to Google Calendar first."
        elif message.lower() in ['no', 'n', 'nope']:
            state.current_step = 'gathering_info'
            state.slots_shown = False
            if 'time' in state.answered_questions:
                state.answered_questions.remove('time')
            state.preferred_time = None
            return "Okay, let's try a different time. When would you like to schedule this meeting?"
    
    # If we updated any state, ask for the next piece of information
    if state_updated:
        next_question = state.get_next_question()
        if next_question:
            if response_parts:
                response_parts.append("")  # Add a blank line
            if next_question == 'purpose':
                response_parts.append("What's the purpose of your meeting?")
            elif next_question == 'duration':
                response_parts.append("How long would you like the meeting to be? (default is 30 minutes)")
            elif next_question == 'time':
                response_parts.append("When would you like to schedule this meeting? You can say things like:\n" +
                                   "• 'tomorrow morning'\n" +
                                   "• 'next Monday at 2pm'\n" +
                                   "• 'June 28 at 10am'")
            elif next_question == 'attendees':
                response_parts.append("Who would you like to invite to this meeting? (Please provide email addresses)")
        return "\n".join(response_parts)
    
    # If no state was updated but we're missing information, ask for it
    next_question = state.get_next_question()
    if next_question:
        if next_question == 'purpose':
            return "What's the purpose of your meeting?"
        elif next_question == 'duration':
            return "How long would you like the meeting to be? (default is 30 minutes)"
        elif next_question == 'time':
            return "When would you like to schedule this meeting? You can say things like:\n" + \
                   "• 'tomorrow morning'\n" + \
                   "• 'next Monday at 2pm'\n" + \
                   "• 'June 28 at 10am'"
        elif next_question == 'attendees':
            return "Who would you like to invite to this meeting? (Please provide email addresses)"
    
    # If we get here, we couldn't understand the input
    return "I'm not sure what information you're providing. Could you please be more specific? Here's what I have so far:\n\n" + \
           (f"📝 Purpose: {state.purpose}\n" if state.purpose else "") + \
           (f"🕒 Duration: {state.meeting_duration} minutes\n" if state.meeting_duration else "") + \
           (f"📅 Time: {state.preferred_time['start'].strftime('%A, %B %d at %I:%M %p')}\n" if state.preferred_time else "") + \
           (f"📧 Attendees: {', '.join(state.attendees)}\n" if state.attendees else "")

def show_voice_interface():
    """Display the voice interface"""
    st.title("AI Meeting Scheduler - Voice Call")
    
    # Phone number input
    phone_number = st.text_input("Enter your phone number (including country code):")
    
    if st.button("Start Call"):
        if not phone_number:
            st.error("Please enter a phone number")
            return
        
        # Initialize voice bot if not already done
        if not st.session_state.voice_bot:
            st.session_state.voice_bot = VoiceBot()
        
        # Start the call
        asyncio.run(start_voice_call(phone_number))
    
    if st.session_state.call_active:
        if st.button("End Call"):
            asyncio.run(end_voice_call())

async def start_voice_call(phone_number):
    """Start a voice call"""
    try:
        call = await st.session_state.voice_bot.start_call(phone_number)
        if call:
            st.session_state.call_active = True
            st.success(f"Call started with {phone_number}")
            
            # Send initial greeting
            greeting = prompts.format_initial_greeting()
            await st.session_state.voice_bot.send_message(greeting)
        else:
            st.error("Failed to start call")
    except Exception as e:
        st.error(f"Error starting call: {str(e)}")

async def end_voice_call():
    """End the voice call"""
    try:
        if st.session_state.voice_bot:
            await st.session_state.voice_bot.end_call()
            st.session_state.call_active = False
            st.success("Call ended")
    except Exception as e:
        st.error(f"Error ending call: {str(e)}")

def main():
    try:
        # Add custom CSS
        st.markdown("""
            <style>
            .auth-container {
                background-color: white;
                padding: 2rem;
                border-radius: 1rem;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                margin: 2rem auto;
                max-width: 600px;
                text-align: center;
            }
            .auth-title {
                color: #1f2937;
                font-size: 1.8rem;
                font-weight: 600;
                margin-bottom: 1rem;
            }
            .auth-description {
                color: #4b5563;
                font-size: 1.1rem;
                margin-bottom: 2rem;
                line-height: 1.6;
            }
            .auth-button {
                display: inline-block;
                background-color: #2563eb;
                color: white !important;
                padding: 0.75rem 2rem;
                border-radius: 0.5rem;
                text-decoration: none;
                font-weight: 500;
                margin-top: 1rem;
                transition: all 0.2s ease;
                border: none;
                cursor: pointer;
            }
            .auth-button:hover {
                background-color: #1d4ed8;
                transform: translateY(-1px);
                text-decoration: none;
            }
            .auth-button:visited {
                color: white !important;
            }
            </style>
        """, unsafe_allow_html=True)

        # Health check endpoint
        if "healthz" in st.query_params:
            st.success("App is healthy")
            return

    # Initialize conversation state if needed
        if 'conversation_state' not in st.session_state:
            st.session_state.conversation_state = ConversationState()
            st.session_state.initialized = False
    
    # Check if this is an OAuth callback
        if 'code' in st.query_params:
            handle_oauth_callback()
            return
        
        # Check for Google Calendar authorization
        if not st.session_state.credentials:
                st.markdown('<div class="auth-container">', unsafe_allow_html=True)
                st.markdown('<h1 class="auth-title">Welcome to AI Meeting Scheduler</h1>', unsafe_allow_html=True)
                st.markdown('<p class="auth-description">Your intelligent assistant for effortless meeting scheduling. Connect your Google Calendar to get started.</p>', unsafe_allow_html=True)
                
                # Features section using Streamlit components
                st.write("")  # Add some spacing
                st.markdown("#### Key Features")
                col1, col2 = st.columns(2)
                
                with col1:
                    st.markdown("🤖 **Natural Language**")
                    st.write("Just chat like you would with a human")
                    
                    st.markdown("✨ **Smart Scheduling**")
                    st.write("Automatic conflict resolution")
                    
                with col2:
                    st.markdown("📅 **Calendar Integration**")
                    st.write("Automatic availability check")
                    
                    st.markdown("📧 **Automated Invites**")
                    st.write("Calendar invites sent automatically")
                
                # Get authorization URL
                flow = Flow.from_client_secrets_file(
                    CLIENT_SECRETS_FILE,
                    scopes=SCOPES,
                    redirect_uri=get_oauth_redirect_uri()
                )
                
                # Generate a secure state parameter
                state = os.urandom(16).hex()
                st.session_state.oauth_state = state
                
                authorization_url, _ = flow.authorization_url(
                    access_type='offline',
                    include_granted_scopes='true',
                    state=state,
                    prompt='consent'
                )
                
                st.markdown(f'<a href="{authorization_url}" class="auth-button">Connect Google Calendar</a>', unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)
        return
        
        # Add tabs for text and voice interfaces
        tab1, tab2 = st.tabs(["Text Chat", "Voice Call"])
        
        with tab1:
            show_chat_interface()
        
        with tab2:
            show_voice_interface()
                    
    except Exception as e:
        st.error(f"An error occurred: {str(e)}")
        logger.error(f"Application error: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main() 