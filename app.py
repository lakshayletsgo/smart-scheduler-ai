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
import nltk
from nltk.tokenize import word_tokenize, sent_tokenize
from nltk.tag import pos_tag
from nltk.chunk import ne_chunk
import logging
import dateparser

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
OAUTH_REDIRECT_URI = 'https://lakshayletsgo-smart-scheduler-ai-streamlit-app-xrbctg.streamlit.app/oauth2callback'

# Store active bot instance
bot = None

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
        self.purpose = None
        self.meeting_duration = 30  # Default duration in minutes
        self.preferred_time = None  # Will be a dict with 'start' and 'end' datetime objects
        self.attendees = set()
        self.answered_questions = set()
        self.slots_shown = False
        self.available_slots = []
        self.selected_slot = None

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
            'slots_shown': self.slots_shown,
            'available_slots': [slot.isoformat() if isinstance(slot, datetime) else slot for slot in self.available_slots],
            'selected_slot': self.selected_slot.isoformat() if isinstance(self.selected_slot, datetime) else self.selected_slot
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
    """Extract meeting details from user message"""
    # Tokenize and tag parts of speech
    tokens = word_tokenize(text)
    pos_tags = pos_tag(tokens)
    named_entities = ne_chunk(pos_tags)
    details = MeetingDetails()
    logger.debug(f"Extracting details from text: {text}")

    # Extract date and time using NLTK's named entity recognition
    for chunk in named_entities:
        if hasattr(chunk, 'label'):
            if chunk.label() == 'DATE' or chunk.label() == 'TIME':
                entity_text = ' '.join([token for token, pos in chunk.leaves()])
                try:
                    parsed_date = dateparser.parse(entity_text, settings={
                        'PREFER_DATES_FROM': 'future',
                        'RELATIVE_BASE': datetime.now(),
                        'PREFER_DAY_OF_MONTH': 'first',
                        'DATE_ORDER': 'DMY'
                    })
                    if not parsed_date or not isinstance(parsed_date, datetime):
                        logger.warning(f"Failed to parse date from: {entity_text}")
                        continue

                    if chunk.label() == 'DATE':
                        details.date = parsed_date.strftime('%Y-%m-%d')
                        logger.debug(f"Extracted date: {details.date}")
                    elif chunk.label() == 'TIME':
                        details.time = parsed_date.strftime('%H:%M')
                        logger.debug(f"Extracted time: {details.time}")
                except Exception as e:
                    logger.error(f"Error parsing date/time: {e}")
                    continue

    # If no date/time found through NER, try direct parsing
    if not details.date or not details.time:
        try:
            parsed_date = dateparser.parse(text, settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now(),
                'PREFER_DAY_OF_MONTH': 'first',
                'DATE_ORDER': 'DMY'
            })
            if parsed_date and isinstance(parsed_date, datetime):
                details.date = parsed_date.strftime('%Y-%m-%d')
                details.time = parsed_date.strftime('%H:%M')
                logger.debug(f"Extracted date/time through direct parsing: {details.date} {details.time}")
        except Exception as e:
            logger.error(f"Error in direct date/time parsing: {e}")

    # Extract email addresses for attendees
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    details.attendees = re.findall(email_pattern, text)
    logger.debug(f"Extracted attendees: {details.attendees}")

    # Extract purpose using improved patterns
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

    # If no purpose was found using patterns, use a simple heuristic
    if not details.purpose:
        for sentence in sentences:
            # Skip sentences that are mainly about date/time
            has_datetime = any(chunk.label() in ['DATE', 'TIME'] for chunk in named_entities if hasattr(chunk, 'label'))
            if not has_datetime and len(sentence.split()) > 3:
                # Extract meaningful parts of the sentence using POS tags
                meaningful_parts = []
                sentence_pos = pos_tag(word_tokenize(sentence))
                for token, pos in sentence_pos:
                    if pos.startswith(('NN', 'VB', 'JJ')) and token.lower() not in ['schedule', 'meeting', 'call']:
                        meaningful_parts.append(token)
                if meaningful_parts:
                    details.purpose = ' '.join(meaningful_parts)
                    break

    # Clean up the purpose
    if details.purpose:
        # Remove common prefixes like "schedule" or "about"
        prefixes_to_remove = ['schedule', 'about', 'for', 'regarding']
        purpose_words = details.purpose.lower().split()
        while purpose_words and purpose_words[0] in prefixes_to_remove:
            purpose_words.pop(0)
        details.purpose = ' '.join(purpose_words).capitalize()

    # Extract duration using patterns
    duration_patterns = [
        r'(\d+)\s*(?:min(?:ute)?s?|hours?)',
        r'(?:for|duration|length)\s+(?:of\s+)?(\d+)\s*(?:min(?:ute)?s?|hours?)',
        r'(?:min(?:ute)?s?|hours?)\s*:\s*(\d+)'
    ]

    for pattern in duration_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                duration = int(match.group(1))
                if 'hour' in match.group().lower():
                    duration *= 60
                details.duration = duration
                logger.debug(f"Extracted duration: {duration} minutes")
                break
            except (ValueError, IndexError):
                continue

    # Check if we have all necessary details
    details.complete = bool(details.purpose and details.date and details.time and details.attendees)
    logger.debug(f"Details complete: {details.complete}")
    logger.debug(f"Final extracted details: {details}")

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
            state.answered_questions.add('purpose')
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
                    
                    # Format confirmation message
                    slot_time = state.selected_slot.strftime("%A, %B %d at %I:%M %p")
                    return jsonify({
                        'response': prompts.format_confirmation(state, slot_time),
                        'state': state.to_dict()
                    })
            except (ValueError, IndexError):
                pass
        
        # Handle confirmation if we're in confirming state
        if state.selected_slot:
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
                state.slots_shown = True
                return jsonify({
                    'response': prompts.format_available_slots(state.available_slots),
                    'state': state.to_dict()
                })
        
        # Get AI response and update state
        response_data = prompts.get_ai_response(message, state)
        if isinstance(response_data, dict):
            # Update state with any new information
            state.purpose = response_data.get('purpose', '')
            state.meeting_duration = response_data.get('duration', 30)
            state.preferred_time = response_data.get('time', None)
            state.attendees = response_data.get('attendees', [])
            state.answered_questions.update(response_data.get('answered_questions', []))
            response = response_data.get('response', '')
        else:
            response = response_data
        
        # If we have all info or AI suggests checking calendar
        if (state.purpose and state.meeting_duration and state.preferred_time and state.attendees) or \
           (state.selected_slot and prompts.should_check_calendar(response)):
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
                response = prompts.format_available_slots(available_slots)
                return jsonify({
                    'response': response,
                    'state': state.to_dict()
                })
        
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
            meeting_datetime = dateparser.parse(f"{data['date']} {data['time']}", settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': datetime.now()
            })
            if not meeting_datetime or not isinstance(meeting_datetime, datetime):
                logger.error(f"Failed to parse meeting datetime from: {data['date']} {data['time']}")
                return jsonify({
                    'success': False,
                    'error': 'Invalid date or time format'
                })
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
