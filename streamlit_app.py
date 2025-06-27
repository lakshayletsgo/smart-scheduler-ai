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
import spacy
import json
import logging
from voice_bot import VoiceBot
import asyncio
from spacy.matcher import Matcher

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Load spaCy model
@st.cache_resource
def load_spacy_model():
    try:
        return spacy.load('en_core_web_sm')
    except OSError:
        # If loading fails, try downloading the model
        try:
            import subprocess
            subprocess.run(['python', '-m', 'spacy', 'download', 'en_core_web_sm'], check=True)
            return spacy.load('en_core_web_sm')
        except Exception as e:
            st.error(f"Failed to load spaCy model: {str(e)}")
            logger.error(f"Error loading spaCy model: {str(e)}")
            # Return a blank English model as fallback
            return spacy.blank('en')

# Initialize the model
nlp = load_spacy_model()

class ConversationState:
    def __init__(self):
        self.reset()
    
    def reset(self):
        """Reset the conversation state"""
        self.meeting_duration = None
        self.preferred_time = None
        self.attendees = []
        self.purpose = None
        self.available_slots = []
        self.current_step = 'initial'
        self.last_question_asked = None
        self.slots_shown = False
        self.selected_slot = None
        self.meeting_confirmed = False
        self.answered_questions = set()

    def is_complete(self):
        """Check if we have all required information"""
        return bool(
            self.purpose and 
            (self.meeting_duration or self.meeting_duration == 30) and  # Allow default duration
            (self.preferred_time or self.selected_slot) and 
            self.attendees
        )

    def get_next_question(self):
        """Get the next question to ask based on missing info and what hasn't been asked"""
        # Priority order for questions
        question_order = ['purpose', 'duration', 'time', 'attendees']
        
        for question in question_order:
            if question not in self.answered_questions:
                if (question == 'purpose' and not self.purpose) or \
                   (question == 'duration' and not self.meeting_duration) or \
                   (question == 'time' and not self.preferred_time and not self.selected_slot) or \
                   (question == 'attendees' and not self.attendees):
                    return question
        return None

# Initialize Google Cloud AI Platform
aiplatform.init(project=os.getenv('GOOGLE_CLOUD_PROJECT'))

# Google OAuth2 Configuration
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), 'client_secrets.json')
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.events']

# Get the deployment URL from Streamlit's environment or use localhost as fallback
def get_oauth_redirect_uri():
    if os.getenv('STREAMLIT_SERVER_URL'):
        base_url = os.getenv('STREAMLIT_SERVER_URL')
    else:
        base_url = 'http://localhost:8501'
    return f"{base_url}/oauth2callback"

# Initialize session state
if 'conversation_state' not in st.session_state:
    st.session_state.conversation_state = ConversationState()
    st.session_state.initialized = False  # Add flag to track initialization
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
        st.write(f"- Time: {state.preferred_time['start'].strftime('%A, %B %d at %I:%M %p') if state.preferred_time else None}")
        st.write(f"- Duration: {state.meeting_duration} minutes")
        st.write(f"- Attendees: {', '.join(state.attendees) if state.attendees else None}")
        st.write(f"- Answered Questions: {state.answered_questions}")
        st.write(f"- Slots Shown: {state.slots_shown}")
    
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
    if details.date and details.time and not state.preferred_time:
        try:
            date_obj = datetime.strptime(details.date, '%Y-%m-%d')
            time_obj = datetime.strptime(details.time, '%H:%M').time()
            preferred_time = datetime.combine(date_obj.date(), time_obj)
            state.preferred_time = {
                'start': preferred_time,
                'end': preferred_time + timedelta(minutes=state.meeting_duration or 30)
            }
            state.answered_questions.add('time')
            response_parts.append(f"I've noted the time: {preferred_time.strftime('%A, %B %d at %I:%M %p')}")
            state_updated = True
        except ValueError as e:
            logger.error(f"Error parsing date/time: {e}")
    
    # Check for duration in the message
    duration_match = re.search(r'(\d+)\s*(hour|hr|min|minutes?)', message.lower())
    if duration_match and not state.meeting_duration:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)
        if unit.startswith('hour') or unit == 'hr':
            amount *= 60
        state.meeting_duration = amount
        state.answered_questions.add('duration')
        response_parts.append(f"Meeting duration set to {amount} minutes")
        state_updated = True
    
    # Update attendees if provided
    if details.attendees and not state.attendees:
        state.attendees.extend(details.attendees)
        state.attendees = list(dict.fromkeys(state.attendees))  # Remove duplicates
        state.answered_questions.add('attendees')
        attendee_list = "\n".join([f"• {attendee}" for attendee in state.attendees])
        response_parts.append(f"Added attendees:\n{attendee_list}")
        state_updated = True
    
    # Update purpose if provided
    if details.purpose and not state.purpose:
        state.purpose = details.purpose
        state.answered_questions.add('purpose')
        response_parts.append(f"Purpose noted: {details.purpose}")
        state_updated = True

    # Log current state for debugging
    logger.debug(f"Current state: purpose={state.purpose}, time={state.preferred_time}, attendees={state.attendees}, duration={state.meeting_duration}")
    
    # If we have all required information, show available slots
    if state.is_complete() and not state.slots_shown:
        creds = st.session_state.credentials
        if creds:
            # If a specific time was requested, don't show available slots
            if state.preferred_time:
                # Create the calendar event directly
                try:
                    event = calendar_utils.create_calendar_event(
                        creds,
                        summary=state.purpose,
                        start_time=state.preferred_time['start'],
                        attendees=state.attendees,
                        duration_minutes=state.meeting_duration or 30
                    )
                    if event:
                        state.slots_shown = True  # Mark as complete
                        return f"Great! I've scheduled the meeting for {state.preferred_time['start'].strftime('%A, %B %d at %I:%M %p')}.\n\nThe calendar invites have been sent to the attendees."
                    else:
                        return "Sorry, that time slot isn't available. Would you like to see available time slots instead?"
                except Exception as e:
                    logger.error(f"Error creating calendar event: {e}")
                    return "Sorry, there was an error creating the calendar event. Please try again."
            else:
                # Only show available slots if no specific time was requested
                available_slots = calendar_utils.find_available_slots(
                    creds,
                    start_time=state.preferred_time['start'] if state.preferred_time else None,
                    duration_minutes=state.meeting_duration or 30,
                    attendees=state.attendees
                )
                state.available_slots = available_slots
                state.slots_shown = True
                state.current_step = 'showing_slots'
                return prompts.format_available_slots(available_slots).replace("<br>", "\n")
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
    
    # If no state was updated, ask for missing information
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
    
    # If we get here, something went wrong with the state
    return "I'm not sure what information you're providing. Could you please be more specific?"

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

class MeetingDetails:
    def __init__(self):
        self.purpose = None
        self.date = None
        self.time = None
        self.attendees = []
        self.complete = False

    def to_dict(self):
        return {
            'purpose': self.purpose,
            'date': self.date,
            'time': self.time,
            'attendees': self.attendees,
            'complete': self.complete
        }

def extract_meeting_details(text):
    doc = nlp(text)
    details = MeetingDetails()
    logger.debug(f"Extracting details from text: {text}")

    # First, extract date and time using spaCy's entity recognition and patterns
    time_patterns = [
        r'(next\s+week)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
        r'(next\s+week)?\s*(morning|afternoon|evening)',
        r'at\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?',
        r'around\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?',
        r'\d{1,2}(?::\d{2})?\s*(?:am|pm)',
    ]

    # Check for time patterns first
    for pattern in time_patterns:
        time_match = re.search(pattern, text.lower())
        if time_match:
            time_text = time_match.group(0)
            logger.debug(f"Found time pattern match: '{time_text}'")
            try:
                base_time = datetime.now()
                logger.debug(f"Initial base_time: {base_time}")

                # Handle relative day expressions
                is_next_week = 'next week' in text.lower()  # Check full text for next week
                logger.debug(f"Is next week mentioned: {is_next_week}")
                
                if is_next_week:
                    base_time += timedelta(days=7)
                    logger.debug(f"Added 7 days for next week. New base_time: {base_time}")
                elif 'tomorrow' in time_text:
                    base_time += timedelta(days=1)
                    logger.debug(f"Added 1 day for tomorrow. New base_time: {base_time}")

                # Handle weekday names
                weekdays = {
                    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                    'friday': 4, 'saturday': 5, 'sunday': 6
                }
                for day, day_num in weekdays.items():
                    if day in time_text:
                        logger.debug(f"Found weekday: {day} (day_num: {day_num})")
                        current_weekday = base_time.weekday()
                        logger.debug(f"Current weekday: {current_weekday}")
                        days_ahead = day_num - current_weekday
                        if days_ahead <= 0:  # If the day has passed this week
                            days_ahead += 7  # Move to next week
                        logger.debug(f"Days ahead before next week check: {days_ahead}")
                        if is_next_week:
                            days_ahead += 7  # Add another week
                            logger.debug(f"Added 7 more days for next week. Days ahead: {days_ahead}")
                        base_time += timedelta(days=days_ahead)
                        logger.debug(f"Final base_time after weekday adjustment: {base_time}")
                        break

                # Handle time of day
                if 'morning' in time_text:
                    parsed_time = base_time.replace(hour=9, minute=0, second=0, microsecond=0)
                elif 'afternoon' in time_text:
                    parsed_time = base_time.replace(hour=14, minute=0, second=0, microsecond=0)
                elif 'evening' in time_text:
                    parsed_time = base_time.replace(hour=17, minute=0, second=0, microsecond=0)
                else:
                    # Try to parse specific time
                    time_only_match = re.search(r'\d{1,2}(?::\d{2})?\s*(?:am|pm)?', time_text)
                    if time_only_match:
                        time_str = time_only_match.group(0)
                        try:
                            parsed_time_obj = parser.parse(time_str)
                            parsed_time = base_time.replace(
                                hour=parsed_time_obj.hour,
                                minute=parsed_time_obj.minute,
                                second=0,
                                microsecond=0
                            )
                        except Exception as e:
                            logger.error(f"Error parsing specific time '{time_str}': {str(e)}")
                            parsed_time = base_time
                    else:
                        parsed_time = base_time

                details.date = parsed_time.strftime('%Y-%m-%d')
                details.time = parsed_time.strftime('%H:%M')
                logger.debug(f"Final extracted date/time: {details.date} {details.time} from '{time_text}'")
                
                # Remove the time expression from text for purpose extraction
                text = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
            except Exception as e:
                logger.error(f"Error parsing time '{time_text}': {str(e)}")
                pass

    # Extract email addresses for attendees
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    details.attendees = re.findall(email_pattern, text)
    
    # Remove email addresses from text for purpose extraction
    for email in details.attendees:
        text = text.replace(email, '').strip()
    
    # Clean up text for purpose extraction by removing common scheduling phrases
    scheduling_phrases = [
        r'i want to schedule',
        r'please schedule',
        r'schedule a',
        r'set up a',
        r'book a',
        r'arrange a',
        r'plan a',
        r'for',
        r'at',
        r'on',
        r'call',
        r'meeting'
    ]
    
    purpose_text = text.lower()
    for phrase in scheduling_phrases:
        purpose_text = re.sub(phrase, '', purpose_text, flags=re.IGNORECASE)
    
    # Clean up the remaining text
    purpose_text = ' '.join(purpose_text.split())
    
    if purpose_text and not any(word in purpose_text.lower() for word in ['tomorrow', 'today', 'next', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
        details.purpose = purpose_text.strip().capitalize()
    
    # Set the complete flag based on having all required information
    details.complete = bool(details.purpose and details.date and details.time and details.attendees)
    logger.debug(f"Extracted details: {details.to_dict()}")
    return details

def main():
    # Initialize conversation state if needed
    if st.session_state.conversation_state is None:
        initialize_conversation_state()
    
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

if __name__ == "__main__":
    main() 