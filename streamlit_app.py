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
import nltk
from nltk.tokenize import word_tokenize, sent_tokenize
from nltk.tag import pos_tag
from nltk.chunk import ne_chunk

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
    nltk.data.find('taggers/averaged_perceptron_tagger')
    nltk.data.find('chunkers/maxent_ne_chunker')
    nltk.data.find('corpora/words')
except LookupError:
    nltk.download('punkt')
    nltk.download('averaged_perceptron_tagger')
    nltk.download('maxent_ne_chunker')
    nltk.download('words')

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

    # Extract date/time using dateparser with improved settings
    try:
        # First try to find explicit time patterns
        time_match = re.search(r'at\s+(\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)', text, re.I)
        time_str = time_match.group(1) if time_match else None

        # Try to find explicit date patterns
        date_match = re.search(r'(?:on\s+)?(next\s+)?(\w+day|tomorrow|next week|today)', text, re.I)
        date_str = date_match.group(0) if date_match else None

        # Look for date/time entities in NLTK's named entities
        for chunk in named_entities:
            if hasattr(chunk, 'label'):
                if chunk.label() in ['DATE', 'TIME']:
                    entity_text = ' '.join([token for token, pos in chunk.leaves()])
                    if not date_str and 'time' not in entity_text.lower():
                        date_str = entity_text
                    elif not time_str and any(t in entity_text.lower() for t in ['am', 'pm', ':'] + [str(i) for i in range(24)]):
                        time_str = entity_text

        # Combine date and time if found separately
        if date_str or time_str:
            parse_str = f"{date_str or 'today'} {time_str or '9am'}"
        else:
            parse_str = text

        parsed_date = parser.parse(
            parse_str,
            settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now(),
                'PREFER_DAY_OF_MONTH': 'first',
                'DATE_ORDER': 'MDY'
            }
        )

        if parsed_date and parsed_date > datetime.now():
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
    st.title("AI Meeting Scheduler")
    st.write("Chat with the AI to schedule your meeting")
    
    # Add debug expander
    with st.expander("Debug Info"):
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
    
    if details.date and details.time:
        try:
            # Combine date and time into a datetime object
            date_str = f"{details.date} {details.time}"
            if state.set_preferred_time(date_str):
                state.answered_questions.add('time')
                response_parts.append(f"Meeting time set to: {state.preferred_time['start'].strftime('%A, %B %d at %I:%M %p')}")
                state_updated = True
                logger.debug(f"Updated time: {state.preferred_time}")
            else:
                response_parts.append("Please provide a future date and time for the meeting.")
        except ValueError as e:
            logger.error(f"Error parsing date/time: {e}")
            response_parts.append("I couldn't understand the date and time. Please provide them in a clearer format.")
        
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
            # Get available slots from calendar
            available_slots = calendar_utils.find_available_slots(
                creds,
                start_time=state.preferred_time['start'] if state.preferred_time else None,
                duration_minutes=state.meeting_duration,
                attendees=list(state.attendees)
            )
            state.available_slots = available_slots
            state.slots_shown = True
            state.current_step = 'showing_slots'
            return prompts.format_available_slots(available_slots)
        else:
            return "Please authorize access to Google Calendar first."
    
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
                response_parts.append("When would you like to schedule this meeting?")
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
            return "When would you like to schedule this meeting?"
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
            st.warning("Please authorize access to Google Calendar to continue")
            authorize_google_calendar()
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