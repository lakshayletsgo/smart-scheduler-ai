# Smart Scheduler AI Agent

A conversational AI chatbot that helps users schedule meetings through natural dialogue using Google's Gemini API and Google Calendar integration.

## Features

- Natural language conversation for meeting scheduling
- Integration with Google Calendar for availability checking
- Context-aware dialogue maintaining user preferences across turns
- Smart time expression parsing (e.g., "late next week", "an hour before my 5 PM meeting")
- Voice input/output support
- Multi-turn conversation memory
- Flexible scheduling suggestions

## Setup

1. Clone the repository
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Set up Google Cloud Project and enable required APIs:
   - Google Calendar API
   - Gemini API
   
4. Create a `.env` file with your credentials:
   ```
   GOOGLE_APPLICATION_CREDENTIALS="path/to/your/credentials.json"
   GOOGLE_CLOUD_PROJECT="your-project-id"
   ```

5. Run the application:
   ```
   python app.py
   ```

## Project Structure

- `app.py`: Main Flask application
- `utils/`
  - `calendar_utils.py`: Google Calendar integration functions
  - `prompt_handler.py`: Gemini API prompt management
  - `time_parser.py`: Smart time expression parsing
  - `voice_handler.py`: Voice input/output processing
- `templates/`: Flask HTML templates
- `static/`: CSS, JavaScript, and other static files

## Usage

1. Start a conversation by describing your meeting needs
2. The AI will ask follow-up questions if needed
3. Confirm suggested time slots
4. The meeting will be scheduled on your Google Calendar

## Voice Features

- Speak naturally to describe your scheduling needs
- The AI will respond with voice output

## Time Expression Examples

- "Schedule a meeting for late next week"
- "Find a slot an hour before my 5 PM Friday meeting"
- "I need a 30-minute slot tomorrow afternoon"
- "Schedule something between Tuesday and Thursday" 