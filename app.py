from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import os
from google.cloud import aiplatform
import google.auth
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
import pickle
from datetime import datetime, timedelta
from google.oauth2 import credentials
from googleapiclient.discovery import build
import calendar_utils
import prompts
from functools import wraps
from urllib.parse import urlencode
import secrets
import asyncio
from voice_bot import VoiceBot
import json
from pathlib import Path
from dotenv import load_dotenv
import re
from dateutil import parser
import spacy
from spacy.matcher import Matcher
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Google OAuth2 Configuration
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), 'client_secrets.json')
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.events']
OAUTH_REDIRECT_URI = 'http://127.0.0.1:5000/oauth2callback'

# Store active bot instance
bot = None

# Load spaCy model for NLP
try:
    nlp = spacy.load('en_core_web_sm')
except OSError:
    import subprocess
    subprocess.run(['python', '-m', 'spacy', 'download', 'en_core_web_sm'])
    nlp = spacy.load('en_core_web_sm')

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def run_async(coro):
    """Helper to run async code in Flask routes"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

aiplatform.init(project=os.getenv('GOOGLE_CLOUD_PROJECT'))

def get_calendar_credentials():
    """Get valid credentials for Google Calendar API."""
    creds = None
    if 'credentials' in session:
        try:
            creds = Credentials(**session['credentials'])
            logger.debug("Retrieved credentials from session")
        except Exception as e:
            logger.error(f"Error creating credentials from session: {e}")
            return None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                session['credentials'] = credentials_to_dict(creds)
                logger.debug("Refreshed expired credentials")
            except Exception as e:
                logger.error(f"Error refreshing credentials: {e}")
                return None
        else:
            logger.warning("No valid credentials found")
            return None

    return creds

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'credentials' not in session:
            return redirect(url_for('authorize'))
        return f(*args, **kwargs)
    return decorated_function

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
        self.answered_questions = set()  # Track which questions have been answered
    
    def update_from_response(self, response_data):
        """Update state based on AI response data"""
        if 'purpose' in response_data and response_data['purpose']:
            self.purpose = response_data['purpose']
            self.answered_questions.add('purpose')
            
        if 'duration' in response_data and response_data['duration']:
            self.meeting_duration = response_data['duration']
            self.answered_questions.add('duration')
            
        if 'time' in response_data and response_data['time']:
            self.preferred_time = response_data['time']
            self.answered_questions.add('time')
            
        if 'attendees' in response_data and response_data['attendees']:
            new_attendees = response_data['attendees']
            if new_attendees:
                self.attendees.extend(new_attendees)
                # Remove duplicates while preserving order
                self.attendees = list(dict.fromkeys(self.attendees))
                self.answered_questions.add('attendees')
    
    def is_complete(self):
        """Check if we have all required information"""
        return bool(
            self.purpose and 
            (self.meeting_duration or self.meeting_duration == 30) and  # Allow default duration
            (self.preferred_time or self.selected_slot) and 
            self.attendees
        )
    
    def get_missing_info(self):
        """Get list of missing information that hasn't been asked about yet"""
        missing = []
        if not self.purpose and 'purpose' not in self.answered_questions:
            missing.append('purpose')
        if not self.meeting_duration and 'duration' not in self.answered_questions:
            missing.append('duration')
        if not self.preferred_time and not self.selected_slot and 'time' not in self.answered_questions:
            missing.append('time')
        if not self.attendees and 'attendees' not in self.answered_questions:
            missing.append('attendees')
        return missing
    
    def get_next_question(self):
        """Get the next question to ask based on missing info and what hasn't been asked"""
        # Priority order for questions
        question_order = ['purpose', 'attendees', 'duration', 'time']
        
        for question in question_order:
            if question not in self.answered_questions:
                if (question == 'purpose' and not self.purpose) or \
                   (question == 'duration' and not self.meeting_duration) or \
                   (question == 'time' and not self.preferred_time and not self.selected_slot) or \
                   (question == 'attendees' and not self.attendees):
                    return question
        return None

    def to_dict(self):
        """Convert state to JSON-serializable dictionary"""
        return {
            'meeting_duration': self.meeting_duration,
            'preferred_time': self.preferred_time,
            'attendees': self.attendees,
            'purpose': self.purpose,
            'available_slots': [slot.isoformat() if slot else None for slot in self.available_slots],
            'current_step': self.current_step,
            'last_question_asked': self.last_question_asked,
            'slots_shown': self.slots_shown,
            'selected_slot': self.selected_slot.isoformat() if self.selected_slot else None,
            'meeting_confirmed': self.meeting_confirmed,
            'answered_questions': list(self.answered_questions)  # Convert set to list
        }

conversation_states = {}

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

    # Extract date and time using spaCy's entity recognition
    for ent in doc.ents:
        if ent.label_ == 'DATE':
            try:
                parsed_date = parser.parse(ent.text)
                details.date = parsed_date.strftime('%Y-%m-%d')
                logger.debug(f"Extracted date: {details.date}")
            except:
                pass
        elif ent.label_ == 'TIME':
            try:
                parsed_time = parser.parse(ent.text)
                details.time = parsed_time.strftime('%H:%M')
                logger.debug(f"Extracted time: {details.time}")
            except:
                pass

    # Extract email addresses for attendees
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    details.attendees = re.findall(email_pattern, text)
    logger.debug(f"Extracted attendees: {details.attendees}")

    # Extract purpose using improved patterns
    purpose_patterns = [
        [{'LOWER': 'schedule'}, {'OP': '*'}, {'POS': 'NOUN'}, {'LOWER': 'meeting'}],
        [{'LOWER': 'schedule'}, {'OP': '*'}, {'POS': 'NOUN'}],
        [{'LOWER': 'about'}, {'OP': '+'}, {'POS': 'NOUN'}],
        [{'LOWER': 'discuss'}, {'OP': '+'}, {'POS': 'NOUN'}],
        [{'LOWER': 'regarding'}, {'OP': '+'}, {'POS': 'NOUN'}],
        [{'LOWER': 'for'}, {'OP': '+'}, {'POS': 'NOUN'}],
        [{'POS': 'NOUN'}, {'LOWER': 'meeting'}],
        [{'POS': 'ADJ'}, {'LOWER': 'meeting'}]
    ]

    matcher = Matcher(nlp.vocab)
    for i, pattern in enumerate(purpose_patterns):
        matcher.add(f"PURPOSE_{i}", [pattern])

    matches = matcher(doc)
    
    # Get the longest matching span for purpose
    if matches:
        longest_match = max(matches, key=lambda x: x[2] - x[1])
        match_id, start, end = longest_match
        purpose_span = doc[start:end]
        
        # Get the sentence containing the purpose
        for sent in doc.sents:
            if purpose_span.start >= sent.start and purpose_span.end <= sent.end:
                # Extract the entire relevant part of the sentence
                relevant_text = []
                for token in sent:
                    if token.dep_ in ['nsubj', 'dobj', 'pobj', 'compound', 'amod'] or token.pos_ in ['NOUN', 'ADJ']:
                        relevant_text.append(token.text)
                
                if relevant_text:
                    details.purpose = ' '.join(relevant_text).strip()
                    break
        
        if not details.purpose:
            details.purpose = purpose_span.text

    # If no purpose was found using patterns, use a simple heuristic
    if not details.purpose:
        for sent in doc.sents:
            # Skip sentences that are mainly about date/time
            has_datetime = any(ent.label_ in ['DATE', 'TIME'] for ent in sent.ents)
            if not has_datetime and len(sent.text.split()) > 3:
                # Extract meaningful parts of the sentence
                meaningful_parts = []
                for token in sent:
                    if token.pos_ in ['NOUN', 'VERB', 'ADJ'] and not token.is_stop:
                        meaningful_parts.append(token.text)
                if meaningful_parts:
                    details.purpose = ' '.join(meaningful_parts)
                    break

    logger.debug(f"Extracted purpose: {details.purpose}")

    # Clean up the purpose
    if details.purpose:
        # Remove common prefixes like "schedule" or "about"
        prefixes_to_remove = ['schedule', 'about', 'for', 'regarding']
        purpose_words = details.purpose.lower().split()
        while purpose_words and purpose_words[0] in prefixes_to_remove:
            purpose_words.pop(0)
        details.purpose = ' '.join(purpose_words).capitalize()

    # Check if we have all necessary details
    details.complete = bool(details.purpose and details.date and details.time and details.attendees)
    logger.debug(f"Details complete: {details.complete}")
    logger.debug(f"Final extracted details: {details.to_dict()}")

    return details

@app.route('/')
def home():
    """Home page route"""
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
        conversation_states[session['session_id']] = ConversationState()
    
    if 'credentials' not in session:
        logger.debug("No credentials in session, redirecting to authorize")
        return redirect(url_for('authorize'))
    return render_template('index.html')

@app.route('/call')
def call_page():
    """Render the voice interface page"""
    if 'credentials' not in session:
        logger.debug("No credentials in session, redirecting to authorize")
        return redirect(url_for('authorize'))
    return render_template('call.html')

@app.route('/authorize')
def authorize():
    """Start the OAuth flow"""
    try:
        flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES)
        flow.redirect_uri = OAUTH_REDIRECT_URI
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true')
        session['state'] = state
        logger.debug("Generated authorization URL")
        return redirect(authorization_url)
    except Exception as e:
        logger.error(f"Error in authorization: {str(e)}")
        return jsonify({'error': 'Authorization failed'}), 500

@app.route('/oauth2callback')
def oauth2callback():
    """Handle the OAuth callback"""
    try:
        state = session['state']
        flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, state=state)
        flow.redirect_uri = OAUTH_REDIRECT_URI
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials
        session['credentials'] = credentials_to_dict(credentials)
        logger.debug("Successfully completed OAuth flow")
        return redirect(url_for('home'))
    except Exception as e:
        logger.error(f"Error in OAuth callback: {str(e)}")
        return jsonify({'error': 'OAuth callback failed'}), 500

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    try:
        data = request.get_json()
        message = data.get('message', '')

        # Initialize conversation state if not exists
        if 'conversation_state' not in session:
            session['conversation_state'] = ConversationState().to_dict()
        
        state = ConversationState()
        state.__dict__.update(session['conversation_state'])
        
        # Handle initial chat message
        if message == "START_CHAT":
            response = "━━━ Welcome! ━━━\n\n👋 Hello! I'm your AI scheduling assistant.\n\nI'll help you schedule your meeting. Let's get started!\n\nWhat's the purpose of your meeting?"
            state.last_question_asked = 'purpose'
            session['conversation_state'] = state.to_dict()
            return jsonify({
                'response': response,
                'state': {
                    'has_purpose': False,
                    'has_duration': False,
                    'has_time': False,
                    'slots_shown': False
                }
            })
        
        # Handle reset command
        if message.lower() in ['reset', 'start over', 'restart']:
            state.reset()
            return jsonify({
                'response': prompts.format_initial_greeting(),
                'state': state.to_dict()
            })
        
        # Handle slot selection if we're showing slots
        if state.slots_shown and state.available_slots:
            try:
                # Check if user message is a slot number
                if message.isdigit() and 1 <= int(message) <= len(state.available_slots):
                    slot_idx = int(message) - 1
                    state.selected_slot = state.available_slots[slot_idx]
                    state.current_step = 'confirming'
                    
                    # Format confirmation message
                    slot_time = state.selected_slot.strftime("%A, %B %d at %I:%M %p")
                    return jsonify({
                        'response': prompts.format_confirmation(state, slot_time),
                        'state': state.to_dict()
                    })
            except (ValueError, IndexError):
                pass
        
        # Handle confirmation if we're in confirming state
        if state.current_step == 'confirming':
            if message.lower() in ['yes', 'sure', 'okay', 'ok', 'y']:
                # Schedule the meeting
                creds = get_calendar_credentials()
                if creds:
                    success, result = create_calendar_event(
                        creds,
                        summary=state.purpose,
                        start_time=state.selected_slot,
                        attendees=state.attendees,
                        duration_minutes=state.meeting_duration or 30
                    )
                    
                    if success:
                        response = prompts.format_success_message(result)
                        # Only reset state after successful scheduling
                        state.reset()
                        return jsonify({
                            'response': response,
                            'state': state.to_dict()
                        })
            elif message.lower() in ['no', 'nope', 'n']:
                state.current_step = 'showing_slots'
                return jsonify({
                    'response': prompts.format_available_slots(state.available_slots),
                    'state': state.to_dict()
                })
        
        # Get AI response and update state
        response_data = prompts.get_ai_response(message, state)
        if isinstance(response_data, dict):
            # Update state with any new information
            state.update_from_response(response_data)
            response = response_data.get('response', '')
        else:
            response = response_data
        
        # If we have all info or AI suggests checking calendar
        if (state.is_complete() and not state.slots_shown) or \
           (state.current_step == 'gathering_info' and prompts.should_check_calendar(response)):
            # Get calendar credentials
            creds = get_calendar_credentials()
            if creds:
                # Get available slots from calendar
                available_slots = calendar_utils.find_available_slots(
                    creds,
                    start_time=state.preferred_time['start'] if state.preferred_time else None,
                    duration_minutes=state.meeting_duration or 30,
                    attendees=state.attendees
                )
                state.available_slots = available_slots
                state.slots_shown = True
                state.current_step = 'showing_slots'
                response = prompts.format_available_slots(available_slots)
                return jsonify({
                    'response': response,
                    'state': state.to_dict()
                })
        
        # Get next question if we're still gathering info
        if state.current_step in ['initial', 'gathering_info']:
            next_question = state.get_next_question()
            if next_question:
                state.current_step = 'gathering_info'
                state.last_question_asked = next_question
                response = prompts.format_info_request(next_question)
                
                # Add current meeting details if we have any
                details = prompts.format_meeting_details(state)
                if details:
                    response += "\n" + details
                
                # Add remaining questions
                missing = state.get_missing_info()
                if len(missing) > 1:  # Don't show if only the current question is missing
                    response += prompts.format_missing_info(missing[1:])  # Skip the current question
        
        return jsonify({
            'response': response,
            'state': state.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error in chat endpoint: {str(e)}")
        return jsonify({
            'error': f'Failed to process chat: {str(e)}'
        }), 500

@app.route('/process_speech', methods=['POST'])
def process_speech():
    """Process the speech transcript and extract meeting details"""
    try:
        data = request.json
        transcript = data.get('transcript', '')
        logger.debug(f"Processing speech transcript: {transcript}")

        if not transcript:
            return jsonify({
                'success': False,
                'error': 'No transcript provided'
            })

        details = extract_meeting_details(transcript)
        logger.debug(f"Extracted meeting details: {details.to_dict()}")
        return jsonify({
            'success': True,
            'details': details.to_dict()
        })
    except Exception as e:
        logger.error(f"Error processing speech: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error processing speech: {str(e)}'
        })

def create_calendar_event(creds, summary, start_time, attendees, duration_minutes=30):
    """Create a Google Calendar event and send invitations."""
    try:
        logger.debug(f"Creating calendar event with summary: {summary}, start_time: {start_time}, attendees: {attendees}")
        service = build('calendar', 'v3', credentials=creds)
        
        # Create event details
        end_time = start_time + timedelta(minutes=duration_minutes)
        event = {
            'summary': summary,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'UTC',
            },
            'attendees': [{'email': email} for email in attendees],
            'reminders': {
                'useDefault': True
            },
            'sendUpdates': 'all'  # This ensures that attendees receive email invitations
        }

        logger.debug(f"Sending calendar event request with data: {event}")
        event = service.events().insert(calendarId='primary', body=event).execute()
        logger.debug(f"Successfully created event with link: {event['htmlLink']}")
        return True, event['htmlLink']
    except Exception as e:
        logger.error(f"Error creating calendar event: {str(e)}")
        return False, str(e)

@app.route('/schedule', methods=['POST'])
@login_required
def schedule_meeting():
    """Schedule the meeting using the extracted details"""
    try:
        data = request.json
        logger.debug(f"Received scheduling request with data: {data}")
        
        if not all(key in data for key in ['purpose', 'date', 'time', 'attendees']):
            logger.warning("Missing required meeting details")
            return jsonify({
                'success': False,
                'error': 'Missing required meeting details'
            })

        creds = get_calendar_credentials()
        if not creds:
            logger.warning("No valid credentials found")
            return jsonify({
                'success': False,
                'error': 'Not authenticated with Google Calendar'
            })

        # Convert date and time to datetime
        try:
            meeting_datetime = parser.parse(f"{data['date']} {data['time']}")
            logger.debug(f"Parsed meeting datetime: {meeting_datetime}")
        except Exception as e:
            logger.error(f"Error parsing datetime: {str(e)}")
            return jsonify({
                'success': False,
                'error': f'Invalid date or time format: {str(e)}'
            })
        
        # Create the calendar event
        success, result = create_calendar_event(
            creds,
            summary=data['purpose'],
            start_time=meeting_datetime,
            attendees=data['attendees']
        )

        if success:
            logger.info("Successfully scheduled meeting")
            return jsonify({
                'success': True,
                'message': 'Meeting scheduled successfully',
                'calendar_link': result
            })
        else:
            logger.error(f"Failed to schedule meeting: {result}")
            return jsonify({
                'success': False,
                'error': f'Failed to schedule meeting: {result}'
            })
    except Exception as e:
        logger.error(f"Unexpected error in schedule_meeting: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Error scheduling meeting: {str(e)}'
        })

@app.route('/start-call', methods=['POST'])
async def start_call():
    """Start a phone call with the specified number"""
    data = request.json
    phone_number = data.get('phone_number')
    
    if not phone_number:
        return jsonify({'error': 'Phone number is required'}), 400
    
    global bot
    bot = VoiceBot()
    result = await bot.start_call(phone_number)
    
    if result:
        return jsonify({
            'status': 'success',
            'call_id': result['call_id']
        })
    else:
        return jsonify({'error': 'Failed to start call'}), 500

@app.route('/send-message', methods=['POST'])
async def send_message():
    """Send a message during the active call"""
    if not bot:
        return jsonify({'error': 'No active call'}), 400
    
    data = request.json
    message = data.get('message')
    
    if not message:
        return jsonify({'error': 'Message is required'}), 400
    
    result = await bot.send_message(message)
    
    if result:
        return jsonify({'status': 'success'})
    else:
        return jsonify({'error': 'Failed to send message'}), 500

@app.route('/end-call', methods=['POST'])
async def end_call():
    """End the current call"""
    if not bot:
        return jsonify({'error': 'No active call'}), 400
    
    result = await bot.end_call()
    
    if result:
        return jsonify({'status': 'success'})
    else:
        return jsonify({'error': 'Failed to end call'}), 500

@app.route('/call-status', methods=['GET'])
async def call_status():
    """Get the current call status"""
    if not bot:
        return jsonify({'error': 'No active call'}), 400
    
    status = await bot.get_call_status()
    
    if status:
        return jsonify(status)
    else:
        return jsonify({'error': 'Failed to get call status'}), 500

if __name__ == '__main__':
    app.run(debug=True)
