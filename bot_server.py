from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from typing import Dict
import asyncio

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store active bots
active_bots: Dict[str, 'AppointmentBot'] = {}

@app.post("/bot/{room_id}/transcribe")
async def transcribe_audio(room_id: str, audio_data: bytes):
    if room_id not in active_bots:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    bot = active_bots[room_id]
    # Process audio data here
    # For now, just return a success message
    return {"status": "success"}

@app.post("/bot/{room_id}/message")
async def send_message(room_id: str, message: str):
    if room_id not in active_bots:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    bot = active_bots[room_id]
    response = await bot.process_conversation(message)
    return {"response": response}

def run_server():
    """Run the FastAPI server"""
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")

if __name__ == "__main__":
    run_server() 