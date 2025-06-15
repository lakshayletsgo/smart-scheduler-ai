from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import os
from google.cloud import aiplatform
import google.auth
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
import pickle
from datetime import datetime
import calendar_utils
import prompts
from functools import wraps
from urllib.parse import urlencode
import secrets
import asyncio
from livekit import api, rtc
from voice_bot import AppointmentBot
import aiohttp
import json
from pathlib import Path
import platform
import socket
from contextlib import asynccontextmanager
import subprocess
import threading
from bot_server import active_bots, run_server

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Initialize LiveKit configuration
LIVEKIT_API_KEY = os.getenv('LIVEKIT_API_KEY')
LIVEKIT_API_SECRET = os.getenv('LIVEKIT_API_SECRET')
LIVEKIT_URL = os.getenv('LIVEKIT_URL')

# Google OAuth2 Configuration
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), 'client_secrets.json')
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.events']
OAUTH_REDIRECT_URI = 'http://127.0.0.1:5000/oauth2callback'

# Store active bots and API client
livekit_api = None
aiohttp_session = None

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

async def get_livekit_api():
    """Get or create LiveKit API client"""
    global livekit_api, aiohttp_session
    if livekit_api is None:
        if aiohttp_session is None:
            aiohttp_session = aiohttp.ClientSession()
        livekit_api = api.LiveKitAPI(
            url=LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
            session=aiohttp_session
        )
    return livekit_api

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

@app.route('/call')
def call_page():
    """Render the call interface page"""
    return render_template('call.html')

def cleanup_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError):
        pass
    try:
        sock.close()
    except (OSError, AttributeError):
        pass

@asynccontextmanager
async def get_session():
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        connector = aiohttp.TCPConnector(force_close=True)
        aiohttp_session = aiohttp.ClientSession(connector=connector)
    try:
        yield aiohttp_session
    finally:
        if not aiohttp_session.closed:
            await aiohttp_session.close()

# Start the bot server in a separate thread when the app starts
def start_bot_server():
    bot_server_thread = threading.Thread(target=run_server, daemon=True)
    bot_server_thread.start()

# Start the bot server when the app starts
start_bot_server()

@app.route('/api/create-room', methods=['POST'])
def create_room():
    """Create a LiveKit room and return tokens"""
    try:
        async def create_room_async():
            async with get_session() as session:
                # Get LiveKit API client with shared session
                livekit_api = api.LiveKitAPI(
                    url=LIVEKIT_URL,
                    api_key=LIVEKIT_API_KEY,
                    api_secret=LIVEKIT_API_SECRET,
                    session=session
                )
                
                # Generate a unique room name
                room_name = f"appointment-{secrets.token_hex(8)}"
                
                # Create the room using the correct API method with CreateRoomRequest
                room_info = await livekit_api.room.create_room(
                    api.CreateRoomRequest(
                        name=room_name,
                        empty_timeout=300  # 5 minutes timeout
                    )
                )
                
                # Create token for the bot
                bot_token = api.AccessToken(
                    api_key=LIVEKIT_API_KEY,
                    api_secret=LIVEKIT_API_SECRET
                ).with_identity("appointment-bot") \
                 .with_name("Appointment Bot") \
                 .with_grants(api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True
                 )).to_jwt()
                
                # Create token for the caller
                caller_token = api.AccessToken(
                    api_key=LIVEKIT_API_KEY,
                    api_secret=LIVEKIT_API_SECRET
                ).with_identity(f"caller-{secrets.token_hex(4)}") \
                 .with_name("Caller") \
                 .with_grants(api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True
                 )).to_jwt()
                
                # Start the bot in this room
                bot = AppointmentBot()
                active_bots[room_name] = bot
                await bot.connect_to_room(LIVEKIT_URL, bot_token)
                
                return {
                    'room': room_name,
                    'token': caller_token,
                    'url': LIVEKIT_URL,
                    'bot_port': 8000  # Use the fixed port of the shared server
                }

        # Windows-specific event loop handling
        if platform.system() == 'Windows':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        else:
            loop = asyncio.get_event_loop()
            
        try:
            result = loop.run_until_complete(create_room_async())
            return jsonify(result)
        finally:
            if platform.system() == 'Windows':
                loop.close()
                
    except Exception as e:
        print(f"Error creating room: {str(e)}")
        return jsonify({'error': 'Failed to create room'}), 500

@app.route('/api/end-call', methods=['POST'])
def end_call():
    """End a call and cleanup resources"""
    try:
        async def end_call_async():
            room_name = request.json.get('room')
            if room_name in active_bots:
                bot = active_bots[room_name]
                # Properly disconnect the bot and cleanup resources
                await bot.disconnect()
                del active_bots[room_name]
                
                # Delete the room using the correct API method
                livekit_api = await get_livekit_api()
                await livekit_api.delete_room(api.DeleteRoomRequest(
                    room=room_name
                ))
                
            return {'success': True}
            
        if platform.system() == 'Windows':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        else:
            loop = asyncio.get_event_loop()
            
        try:
            result = loop.run_until_complete(end_call_async())
            return jsonify(result)
        finally:
            if platform.system() == 'Windows':
                loop.close()
        
    except Exception as e:
        print(f"Error ending call: {str(e)}")
        return jsonify({'error': 'Failed to end call'}), 500

@app.route('/authorize')
def authorize():
    # Clear any existing OAuth state
    session.pop('oauth_state', None)
    
    # Generate and store a random state parameter
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    
    try:
        if not os.path.exists(CLIENT_SECRETS_FILE):
            return 'Error: client_secrets.json file not found. Please set up OAuth 2.0 credentials.', 500
            
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            redirect_uri=OAUTH_REDIRECT_URI
        )
        
        authorization_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=state,
            prompt='consent'  
        )
        
        return redirect(authorization_url)
        
    except Exception as e:
        return f'Error starting OAuth flow: {str(e)}', 500

@app.route('/oauth2callback')
def oauth2callback():
    # Verify state parameter
    state = request.args.get('state', None)
    stored_state = session.get('oauth_state', None)
    
    if not state or not stored_state or state != stored_state:
        return 'Invalid state parameter. Please try again.', 400
    
    try:
        if not os.path.exists(CLIENT_SECRETS_FILE):
            return 'Error: client_secrets.json file not found', 500
            
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            redirect_uri=OAUTH_REDIRECT_URI,
            state=state
        )
        
        # Fetch OAuth 2.0 tokens
        flow.fetch_token(
            authorization_response=request.url,
            code=request.args.get('code')
        )
        
        # Get credentials and save to session
        credentials = flow.credentials
        session['credentials'] = credentials_to_dict(credentials)
        
        # Clear OAuth state
        session.pop('oauth_state', None)
        
        return redirect(url_for('home'))
        
    except Exception as e:
        error_details = {
            'error': str(e),
            'error_description': getattr(e, 'message', 'Unknown error occurred'),
            'state': state,
            'code': request.args.get('code'),
            'scope': request.args.get('scope')
        }
        return f'Failed to complete authentication: {json.dumps(error_details, indent=2)}', 400

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

# Cleanup function for the aiohttp session
@app.teardown_appcontext
def cleanup_session(exception=None):
    global aiohttp_session
    if aiohttp_session is not None and not aiohttp_session.closed:
        if platform.system() == 'Windows':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        else:
            loop = asyncio.get_event_loop()
            
        try:
            async def close_session():
                await aiohttp_session.close()
                
            loop.run_until_complete(close_session())
        finally:
            if platform.system() == 'Windows':
                loop.close()
            aiohttp_session = None

if __name__ == '__main__':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(host='127.0.0.1', port=5000, debug=True)
