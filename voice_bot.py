import asyncio
import json
import os
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv
from livekit import rtc
from calendar_utils import CalendarManager

load_dotenv()

# Configure Gemini AI
genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

class AppointmentBot:
    def __init__(self):
        self.room = None
        self.calendar_manager = CalendarManager()
        self.client_info = {}
        self.current_conversation = {
            'name': None,
            'date': None,
            'time': None,
            'reason': None,
            'is_existing_client': None
        }
        
    async def connect_to_room(self, url: str, token: str):
        """Connect to a LiveKit room"""
        room_options = rtc.RoomOptions(
            auto_subscribe=True,
        )
        
        self.room = rtc.Room(options=room_options)
        
        # Set up event listeners using the event emitter pattern
        self.room.on('track_subscribed', self._on_track_subscribed)
        self.room.on('participant_connected', self._on_participant_connected)
        
        # Connect to the room
        await self.room.connect(url, token)
        
    async def disconnect(self):
        """Cleanup resources when disconnecting."""
        if self.room:
            await self.room.disconnect()

    def _on_track_subscribed(self, track, publication, participant):
        """Handle subscribed tracks"""
        if isinstance(track, rtc.RemoteAudioTrack):
            # Create an audio stream to process the audio
            audio_stream = rtc.AudioStream(track)
            asyncio.create_task(self._process_audio(audio_stream))
            
    def _on_participant_connected(self, participant):
        """Handle new participant connections"""
        print(f"New participant connected: {participant.identity}")
        # Send welcome message
        asyncio.create_task(self.send_response(
            "Hello! I'm your appointment scheduling assistant. May I know your name?"
        ))
            
    async def _process_audio(self, audio_stream):
        """Process audio from the stream"""
        async for frame in audio_stream:
            # Here we would normally process the audio frame
            # For now, we'll just print that we received audio
            print("Received audio frame")
            
    async def process_conversation(self, text: str):
        """Process the conversation using Gemini AI"""
        conversation_prompt = f"""
        You are an appointment scheduling assistant. Your task is to help schedule appointments by gathering necessary information from the conversation.
        
        Current conversation state: {json.dumps(self.current_conversation)}
        User said: {text}
        
        Extract the following information if present:
        1. Name
        2. Date and time for appointment
        3. Reason for appointment
        4. Any indication if they are an existing client
        
        Format your response as a JSON string with two fields:
        1. "extracted_info": dictionary containing any extracted information
        2. "response": your natural language response to the user
        """
        
        response = await model.generate_content(conversation_prompt)
        try:
            # Parse the AI response
            response_data = json.loads(response.text)
            # Update conversation state
            if 'extracted_info' in response_data:
                self.current_conversation.update(response_data['extracted_info'])
            
            # Check if we have all required information
            if self._is_appointment_info_complete():
                return await self._schedule_appointment()
            
            return response_data.get('response', 'Could you please provide more information?')
            
        except json.JSONDecodeError:
            # Fallback in case the AI response isn't properly formatted JSON
            return "I apologize, but I'm having trouble understanding. Could you please repeat that?"
        
    def _is_appointment_info_complete(self) -> bool:
        """Check if we have all required information for scheduling"""
        return all(self.current_conversation.values())
        
    async def _schedule_appointment(self) -> str:
        """Schedule the appointment when all information is collected"""
        try:
            # Use calendar_manager to schedule the appointment
            success = await self.calendar_manager.schedule_appointment(
                self.current_conversation['name'],
                self.current_conversation['date'],
                self.current_conversation['time'],
                self.current_conversation['reason']
            )
            
            if success:
                return f"Great! I've scheduled your appointment for {self.current_conversation['date']} at {self.current_conversation['time']}. We'll see you then!"
            else:
                return "I apologize, but that time slot is not available. Would you like to try a different time?"
                
        except Exception as e:
            return f"I apologize, but there was an error scheduling your appointment. Please try again."
            
    async def send_response(self, text: str):
        """Send response back to the caller"""
        if self.room and self.room.local_participant:
            # Create a data packet for the response
            data_packet = rtc.DataPacket(
                payload=json.dumps({
                    'type': 'speech',
                    'text': text
                }).encode(),
                kind=rtc.DataPacketKind.KIND_RELIABLE
            )
            await self.room.local_participant.publish_data(data_packet) 