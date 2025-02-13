import openai
from twilio.twiml.voice_response import VoiceResponse, Gather
from flask import Flask, request, jsonify
import datetime
import json
import pytz
import os
from dotenv import load_dotenv
from flask_cors import CORS
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Load environment variables
load_dotenv()

# Retrieve environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BUSINESS_HOURS = {'start': 9, 'end': 17}
WEEKEND_DAYS = (5, 6)
MINIMUM_NOTICE = datetime.timedelta(hours=1)
MAXIMUM_FUTURE_DAYS = 60
MEETINGS_FILE = 'meetings.json'

# VoiceBot class to handle AI responses and meeting scheduling
class VoiceBot:
    def __init__(self):
        self.openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        self.scheduler = MeetingScheduler()
        self.conversation_state = {}

    def detect_intent(self, text):
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4-mini",
                messages=[ 
                    {"role": "system", "content": "Analyze if the user wants to schedule a meeting or just have a conversation. Respond with either 'scheduling' or 'conversation'."},
                    {"role": "user", "content": text}
                ]
            )
            return response.choices[0].message.content.strip().lower()
        except openai.error.OpenAIError as e:
            logging.error(f"Error detecting intent: {e}")
            return "conversation"  # Default to 'conversation' in case of error

    def get_ai_response(self, text, phone_number):
        try:
            # Use the phone number as a conversation reference ID
            conversation = self.conversation_state.get(phone_number, [])
            conversation.append({"role": "user", "content": text})
            intent = self.detect_intent(text)
            system_prompt = (
                "You are a voice assistant that helps schedule meetings. Keep responses concise and clear. Ask for specific details needed for scheduling."
                if intent == 'scheduling' 
                else "You are a friendly voice assistant. Engage in natural conversation while remembering you can help schedule meetings if needed."
            )
            response = self.openai_client.chat.completions.create(
                model="gpt-4-mini",
                messages=[{"role": "system", "content": system_prompt}, *conversation]
            )
            ai_response = response.choices[0].message.content.strip()
            conversation.append({"role": "assistant", "content": ai_response})
            self.conversation_state[phone_number] = conversation
            return ai_response, intent
        except openai.error.OpenAIError as e:
            logging.error(f"Error getting AI response: {e}")
            return "Sorry, I couldn't process your request.", "conversation"

    def schedule_meeting(self, details):
        try:
            success, message = self.scheduler.schedule_meeting(
                summary=details['title'],
                start_time=details['datetime'],
                duration_minutes=details.get('duration', 60),
                attendees=details.get('attendees', [])
            )
            return success, message
        except Exception as e:
            logging.error(f"Error scheduling meeting: {e}")
            return False, f"Failed to schedule meeting: {str(e)}"


# MeetingScheduler class to handle scheduling logic
class MeetingScheduler:
    def __init__(self):
        self.timezone = pytz.timezone('UTC')
        self.load_meetings()

    def load_meetings(self):
        if os.path.exists(MEETINGS_FILE):
            with open(MEETINGS_FILE, 'r') as f:
                self.meetings = json.load(f)
        else:
            self.meetings = []
            self.save_meetings()

    def save_meetings(self):
        with open(MEETINGS_FILE, 'w') as f:
            json.dump(self.meetings, f, indent=2)

    def is_valid_meeting_time(self, start_datetime, end_datetime):
        now = datetime.datetime.now(self.timezone)
        if start_datetime < now + MINIMUM_NOTICE:
            return False, "Meeting must be scheduled at least 1 hour in advance"
        if start_datetime > now + datetime.timedelta(days=MAXIMUM_FUTURE_DAYS):
            return False, "Cannot schedule meetings more than 60 days in advance"
        if not (BUSINESS_HOURS['start'] <= start_datetime.hour < BUSINESS_HOURS['end']):
            return False, "Meetings can only be scheduled during business hours (9 AM - 5 PM)"
        if end_datetime.hour >= BUSINESS_HOURS['end']:
            return False, "Meeting would extend beyond business hours"
        if start_datetime.weekday() in WEEKEND_DAYS:
            return False, "Meetings cannot be scheduled on weekends"
        return True, "Time slot is valid"

    def check_conflicts(self, start_datetime, end_datetime):
        for meeting in self.meetings:
            meeting_start = datetime.datetime.fromisoformat(meeting['start'])
            meeting_end = datetime.datetime.fromisoformat(meeting['end'])
            if (start_datetime < meeting_end and end_datetime > meeting_start):
                return True
        return False

    def schedule_meeting(self, summary, start_time, duration_minutes=60, attendees=None):
        try:
            start_datetime = datetime.datetime.fromisoformat(start_time).replace(tzinfo=self.timezone)
            end_datetime = start_datetime + datetime.timedelta(minutes=duration_minutes)

            is_valid, message = self.is_valid_meeting_time(start_datetime, end_datetime)
            if not is_valid:
                return False, message

            if self.check_conflicts(start_datetime, end_datetime):
                return False, "Time slot is not available"

            meeting = {
                'summary': summary,
                'start': start_datetime.isoformat(),
                'end': end_datetime.isoformat(),
                'attendees': attendees or []
            }

            self.meetings.append(meeting)
            self.save_meetings()
            return True, f"Meeting scheduled successfully for {start_datetime.strftime('%Y-%m-%d %H:%M')}"
        except Exception as e:
            logging.error(f"Error scheduling meeting: {e}")
            return False, f"Failed to schedule meeting: {str(e)}"


def extract_meeting_details(text):
    try:
        response = bot.openai_client.chat.completions.create(
            model="gpt-4-mini",
            messages=[
                {"role": "system", "content": "Extract meeting details in JSON format with keys: title, datetime, duration, attendees"},
                {"role": "user", "content": text}
            ]
        )
        details = json.loads(response.choices[0].message.content)
        return details
    except Exception as e:
        logging.error(f"Error extracting meeting details: {e}")
        return None


bot = VoiceBot()

# Endpoint to get all meetings
@app.route("/meetings", methods=['GET'])
def get_meetings():
    try:
        if os.path.exists(MEETINGS_FILE):
            with open(MEETINGS_FILE, 'r') as f:
                meetings = json.load(f)
            return jsonify(meetings)
        return jsonify([])
    except Exception as e:
        logging.error(f"Error fetching meetings: {e}")
        return jsonify({"error": "Failed to fetch meetings"}), 500

# Endpoint to initiate a conversation (no actual phone call)
@app.route("/voice", methods=['POST'])
def handle_conversation():
    try:
        # Get the phone number from the request (used as a reference ID for the conversation)
        data = request.json
        phone_number = data.get('phone_number')
        
        if not phone_number:
            return jsonify({"error": "Phone number is required"}), 400

        # Initiate the voice interaction
        response = VoiceResponse()
        gather = Gather(input='speech', timeout=3, action='/process_speech')
        gather.say("Hello! I'm your AI assistant. How can I help you today?")
        response.append(gather)

        return str(response)
    except Exception as e:
        logging.error(f"Error initiating conversation: {e}")
        return jsonify({"error": str(e)}), 500

# Webhook that handles Twilio voice responses (for speech gathering)
@app.route("/process_speech", methods=['POST'])
def process_speech():
    phone_number = request.values.get('PhoneNumber')
    speech_result = request.values.get('SpeechResult')

    if not phone_number or not speech_result:
        logging.error("Missing PhoneNumber or SpeechResult in the request")
        return jsonify({"error": "Invalid request"}), 400

    ai_response, intent = bot.get_ai_response(speech_result, phone_number)
    response = VoiceResponse()

    if intent == 'scheduling':
        details = extract_meeting_details(speech_result)
        if details:
            success, message = bot.schedule_meeting(details)
            response.say(message)
        else:
            response.say(ai_response)
    else:
        response.say(ai_response)

    gather = Gather(input='speech', timeout=3, action='/process_speech')
    response.append(gather)
    return str(response)

if __name__ == "__main__":
    # Use the dynamic port provided by Render
    port = int(os.environ.get("PORT", 5000))  # Default to 5000 if not set
    app.run(host='0.0.0.0', port=port, debug=True)
