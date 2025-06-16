import os
import asyncio
import json
from dotenv import load_dotenv
import bland

load_dotenv()

BLAND_API_KEY = os.getenv('BLAND_API_KEY')
bland.api_key = BLAND_API_KEY

class VoiceBot:
    def __init__(self, voice_id="default"):
        self.voice_id = voice_id
        self.call = None
        
    async def start_call(self, phone_number):
        """Start a phone call using bland.ai"""
        try:
            self.call = await asyncio.to_thread(
                bland.start_call,
                phone_number=phone_number,
                voice_id=self.voice_id,
                reduce_latency=True,
                wait_for_greeting=False
            )
            return self.call
        except Exception as e:
            print(f"Error starting call: {e}")
            return None

    async def send_message(self, message):
        """Send a message during the call"""
        if not self.call:
            return False
        
        try:
            response = await asyncio.to_thread(
                bland.send_message,
                call_id=self.call['call_id'],
                message=message
            )
            return response
        except Exception as e:
            print(f"Error sending message: {e}")
            return False

    async def end_call(self):
        """End the current call"""
        if not self.call:
            return False
        
        try:
            response = await asyncio.to_thread(
                bland.end_call,
                call_id=self.call['call_id']
            )
            self.call = None
            return response
        except Exception as e:
            print(f"Error ending call: {e}")
            return False

    async def get_call_status(self):
        """Get the current call status"""
        if not self.call:
            return None
        
        try:
            status = await asyncio.to_thread(
                bland.get_call_status,
                call_id=self.call['call_id']
            )
            return status
        except Exception as e:
            print(f"Error getting call status: {e}")
            return None 