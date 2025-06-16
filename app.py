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
        self.meeting_duration = None
        self.preferred_time = None
        self.attendees = []
        self.purpose = None
        self.available_slots = []

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
    data = request.json
    user_message = data.get('message', '')
    session_id = session.get('session_id')
    
    if not session_id or session_id not in conversation_states:
        return jsonify({'error': 'Invalid session'}), 400
    
    state = conversation_states[session_id]
    
    # Process the message and update state
    # This is where you'd integrate with your AI model
    response = "Message received: " + user_message
    
    return jsonify({
        'response': response,
        'state': {
            'meeting_duration': state.meeting_duration,
            'preferred_time': state.preferred_time,
            'attendees': state.attendees,
            'purpose': state.purpose,
            'available_slots': state.available_slots
        }
    })

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
