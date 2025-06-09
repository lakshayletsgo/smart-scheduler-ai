from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import os
from google.cloud import aiplatform
import google.auth
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle
from datetime import datetime
import calendar_utils
import prompts
from functools import wraps
from urllib.parse import urlencode
import secrets

app = Flask(__name__)
app.secret_key = os.urandom(24)

aiplatform.init(project=os.getenv('GOOGLE_CLOUD_PROJECT'))

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.events']

OAUTH_REDIRECT_URI = 'http://127.0.0.1:5000/oauth2callback'

def get_calendar_credentials():
    creds = None
    if 'credentials' in session:
        creds = Credentials(**session['credentials'])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            session['credentials'] = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
        else:
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

@app.route('/')
def home():
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
        conversation_states[session['session_id']] = ConversationState()
    
    if 'credentials' not in session:
        return redirect(url_for('authorize'))
    return render_template('index.html')

@app.route('/authorize')
def authorize():
    session.pop('oauth_state', None)
    
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    
    flow = InstalledAppFlow.from_client_secrets_file(
        os.getenv('GOOGLE_APPLICATION_CREDENTIALS'),
        SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI
    )
    authorization_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        state=state
    )
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = request.args.get('state', None)
    stored_state = session.get('oauth_state', None)
    
    if not state or not stored_state or state != stored_state:
        return 'Invalid state parameter. Please try again.', 400
    
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            os.getenv('GOOGLE_APPLICATION_CREDENTIALS'),
            SCOPES,
            redirect_uri=OAUTH_REDIRECT_URI
        )
        
        flow.fetch_token(authorization_response=request.url)
        
        credentials = flow.credentials
        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        
        session.pop('oauth_state', None)
        
        return redirect(url_for('home'))
    except Exception as e:
        return f'Failed to complete authentication: {str(e)}', 400

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    user_input = request.json.get('message', '')
    session_id = session.get('session_id')
    conversation_state = conversation_states.get(session_id)
    
    if not conversation_state:
        conversation_state = ConversationState()
        conversation_states[session_id] = conversation_state
    
    response = prompts.get_ai_response(user_input, conversation_state)
    
    time_slots = None
    if prompts.should_check_calendar(response):
        try:
            creds = Credentials(**session['credentials'])
            available_slots = calendar_utils.find_available_slots(
                creds,
                conversation_state.meeting_duration or 30,
                conversation_state.preferred_time
            )
            conversation_state.available_slots = available_slots
            time_slots = [slot.strftime("%A, %B %d at %I:%M %p") for slot in available_slots]
            response = prompts.format_available_slots(available_slots)
        except Exception as e:
            print(f"Error checking calendar: {str(e)}")
            response = "I apologize, but I encountered an error while checking the calendar. Could you please try again?"
            time_slots = []
    
    return jsonify({
        'response': response,
        'has_calendar_data': bool(time_slots),
        'time_slots': time_slots,
        'state': {
            'has_purpose': bool(conversation_state.purpose),
            'has_duration': bool(conversation_state.meeting_duration),
            'has_time': bool(conversation_state.preferred_time),
            'has_attendees': bool(conversation_state.attendees),
            'slots_shown': bool(conversation_state.available_slots)
        }
    })

@app.route('/schedule', methods=['POST'])
@login_required
def schedule_meeting():
    slot_data = request.json
    slot_index = slot_data.get('slot_index')
    session_id = session.get('session_id')
    conversation_state = conversation_states.get(session_id)
    
    if not conversation_state or slot_index is None or slot_index >= len(conversation_state.available_slots):
        return jsonify({
            'success': False, 
            'message': 'Invalid time slot. Please choose a valid option.'
        })
    
    selected_time = conversation_state.available_slots[slot_index]
    creds = Credentials(**session['credentials'])
    
    success, message = calendar_utils.schedule_meeting(
        creds,
        start_time=selected_time,
        duration=conversation_state.meeting_duration or 30,
        attendees=conversation_state.attendees,
        purpose=conversation_state.purpose or "Scheduled Meeting"
    )
    
    if success:
        conversation_states[session_id] = ConversationState()
    
    return jsonify({
        'success': success,
        'message': message
    })

if __name__ == '__main__':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(host='127.0.0.1', port=5000, debug=True) 