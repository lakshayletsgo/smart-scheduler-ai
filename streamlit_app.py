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
import en_core_web_sm

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

class ConversationState:
    def __init__(self):
        self.purpose = None
        self.meeting_duration = 30  # Default duration in minutes
        self.preferred_time = None
        self.attendees = set()
        self.answered_questions = set()
        self.current_step = 'initial'
        self.slots_shown = False
        self.available_slots = []
        self.last_question_asked = None

    def reset(self):
        self.__init__()

    def to_dict(self):
        return {
            'purpose': self.purpose,
            'meeting_duration': self.meeting_duration,
            'preferred_time': self.preferred_time,
            'attendees': list(self.attendees),
            'answered_questions': list(self.answered_questions),
            'current_step': self.current_step,
            'slots_shown': self.slots_shown,
            'available_slots': self.available_slots,
            'last_question_asked': self.last_question_asked
        }

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

def extract_meeting_details(text):
    """Extract meeting details using regex patterns"""
    details = MeetingDetails()
    
    # Extract duration using regex
    duration_match = re.search(r'(\d+)\s*(hour|hr|min|minutes?)', text.lower())
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)
        if unit.startswith('hour') or unit == 'hr':
            details.duration = amount * 60
        else:
            details.duration = amount

    # Extract date/time using dateparser
    try:
        parsed_date = parser.parse(text, settings={'PREFER_DATES_FROM': 'future'})
        if parsed_date:
            details.date = parsed_date.strftime('%Y-%m-%d')
            details.time = parsed_date.strftime('%H:%M')
    except:
        pass

    # Extract purpose (simple heuristic - take the first sentence that doesn't contain time/date)
    sentences = re.split(r'[.!?]+', text)
    for sentence in sentences:
        # Skip sentences with time/date indicators
        if not re.search(r'\b(today|tomorrow|next|at|on|pm|am|:\d{2}|\d{1,2}(?::\d{2})?)\b', sentence.lower()):
            purpose = sentence.strip()
            if purpose:
                details.purpose = purpose
                break

    # Extract email addresses
    details.attendees = re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    
    return details

# Initialize session state
if 'conversation_state' not in st.session_state:
    st.session_state.conversation_state = ConversationState()
    st.session_state.initialized = False
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