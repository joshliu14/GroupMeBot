from flask import Flask, request
import os
import json
from datetime import datetime
import pytz
from google import genai
from google.genai import types
from dotenv import load_dotenv
import requests
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
scheduler.start()


def current_eastern_time():
    """
    Returns the current date and time in Eastern Time,
    formatted with day of week, month, day, year, and time.
    """
    eastern = pytz.timezone("US/Eastern")
    now_utc = datetime.utcnow()
    now_eastern = now_utc.replace(tzinfo=pytz.utc).astimezone(eastern)
    return now_eastern.strftime("%A, %B %d, %Y at %I:%M %p ET")

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv('/home/joshliu/mysite/.env')

GROUPME_BOT_ID = os.getenv('GROUPME_BOT_ID')
ACCESS_TOKEN = os.getenv('GROUPME_ACCESS_TOKEN')
GROUP_ID = "109980984"
GROUPME_API_URL = 'https://api.groupme.com/v3/bots/post'
GROUPME_MESSAGES_URL = f"https://api.groupme.com/v3/groups/{GROUP_ID}/messages"

TRIGGER_WORDS = ["hey jarvis"]

# ---------------------------
# Load static JSON data
# ---------------------------
with open("data.json", "r") as f:
    data = json.load(f)
members = ", ".join(data["members"])
cleaning_tasks = ", ".join(data["cleaning"])
dish_day_assignments = ", ".join(f"{k}: {v}" for k, v in data["dishes"].items())

# Initialize lists for shopping and events (in-memory, could be DB)
shopping_list = []
events_list = []
cleaning_schedule = data.get("cleaning_schedule", cleaning_tasks)

# ---------------------------
# Initialize Flask and Gemini
# ---------------------------
app = Flask(__name__)
client = genai.Client()

# ---------------------------
# Define functions/tools for Gemini
# ---------------------------
tools_list = []

# --- Reminders ---
schedule_reminder_function = {
    "name": "schedule_reminder",
    "description": "Schedule a reminder for a roommate",
    "parameters": {
        "type": "object",
        "properties": {
            "time": {"type": "string", "description": "ISO 8601 datetime string"},
            "message": {"type": "string", "description": "Reminder text"},
            "user": {"type": "string", "description": "User requesting the reminder"}
        },
        "required": ["time", "message"]
    }
}
tools_list.append(schedule_reminder_function)

# --- Shopping List ---
add_to_shopping_list_function = {
    "name": "add_to_shopping_list",
    "description": "Add one or more items to the shared shopping list.",
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of item names to add"
            },
            "quantity": {"type": "string", "description": "Optional amount or quantity for all items"},
            "requested_by": {"type": "string", "description": "Who requested the items"}
        },
        "required": ["items"]
    }
}
get_shopping_list_function = {
    "name": "get_shopping_list",
    "description": "Retrieve the current shared shopping list.",
    "parameters": {"type": "object", "properties": {}}
}
clear_shopping_list_function = {
    "name": "clear_shopping_list",
    "description": "Clear the entire shopping list.",
    "parameters": {"type": "object", "properties": {}}
}
remove_from_shopping_list_function = {
    "name": "remove_from_shopping_list",
    "description": "Remove one or more items from the shopping list.",
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of item names to remove"
            }
        },
        "required": ["items"]
    }
}
tools_list += [add_to_shopping_list_function, get_shopping_list_function, 
               clear_shopping_list_function, remove_from_shopping_list_function]

# --- Events ---
schedule_event_function = {
    "name": "schedule_event",
    "description": "Schedule a group event for all or some roommates.",
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date of the event (YYYY-MM-DD)"},
            "time": {"type": "string", "description": "Time of the event (HH:MM)"},
            "title": {"type": "string", "description": "Event title"},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of roommates invited"
            }
        },
        "required": ["date", "time", "title"]
    }
}
get_events_function = {
    "name": "get_events",
    "description": "Get all scheduled events.",
    "parameters": {"type": "object", "properties": {}}
}
tools_list += [schedule_event_function, get_events_function]

# --- Cleaning Schedule ---
get_cleaning_schedule_function = {
    "name": "get_cleaning_schedule",
    "description": "Retrieve the current cleaning rotation schedule.",
    "parameters": {"type": "object", "properties": {}}
}
tools_list.append(get_cleaning_schedule_function)

# Create tools object for Gemini
tools = types.Tool(function_declarations=tools_list)

# ---------------------------
# Create chat session with persistent memory
# ---------------------------
def create_system_instruction():
    time = current_eastern_time()
    return f"""
You are Jarvis, inspired by Iron Man's AI assistant.
Your role is to assist members of the C1 chatroom (also known as CCA1 or Concord Cave of Adullam 1).
Be concise, reliable, and helpful while analyzing conversations, remembering information, performing tasks, and answering questions.

Guidelines:
- Be clear, professional, and a little witty, but never misleading.
- Remember previous conversations and context from this chat session.
- You are not allowed use markdown formatting under any circumstance (do not surround words with **).
- Respond using normal text.
- When you indent, always use four regular spaces instead of the special characters or commands.
- When listing items, always use the single dash as the bullet point.
- The current date and time right now is {time}.
- Members of the chatroom are: {members}
- List of cleaning tasks: {cleaning_tasks}
- Dish day assignments: {dish_day_assignments}
"""

# Initialize chat session once at startup
chat_session = None

def get_chat_session():
    global chat_session
    if chat_session is None:
        chat_session = client.chats.create(model="gemini-2.5-flash")
    return chat_session

# ---------------------------
# Helper functions
# ---------------------------
def send_groupme_message(text):
    if not GROUPME_BOT_ID:
        return False
    
    if not text or not text.strip():
        return False
    
    payload = {'bot_id': GROUPME_BOT_ID, 'text': text.strip()}
    try:
        response = requests.post(GROUPME_API_URL, json=payload)
        if response.status_code != 202:
            print("error")
        else:
            print("success")
        return response.status_code == 202
    except:
        return False

def call_backend_function(name, args):
    """Map Gemini function calls to Python functions"""
    try:
        if name == "schedule_reminder":
            return schedule_reminder(**args)
        elif name == "add_to_shopping_list":
            return add_to_shopping_list(**args)
        elif name == "get_shopping_list":
            return get_shopping_list()
        elif name == "clear_shopping_list":
            return clear_shopping_list()
        elif name == "remove_from_shopping_list":
            return remove_from_shopping_list(**args)
        elif name == "schedule_event":
            return schedule_event(**args)
        elif name == "get_events":
            return get_events()
        elif name == "get_cleaning_schedule":
            return get_cleaning_schedule()
        else:
            return f"Unknown function: {name}"
    except Exception as e:
        return f"Error executing {name}: {str(e)}"

# ---------------------------
# Roommate functions
# ---------------------------
def schedule_reminder(time, message, user=None):
    """
    Schedule a reminder to send a message at a specific time.
    - time: ISO 8601 datetime string (e.g., "2025-09-04T16:30:00")
    - message: reminder text
    - user: optional name of the user who requested the reminder
    """
    try:
        # Parse the ISO 8601 time string into a datetime object
        run_time = datetime.fromisoformat(time)

        # Define the job that will run at the scheduled time
        def job():
            user_text = f" (requested by {user})" if user else ""
            full_message = f"⏰ Reminder{user_text}: {message}"
            send_groupme_message(full_message)

        # Add the job to the scheduler
        scheduler.add_job(job, "date", run_date=run_time)

        user_text = f" for {user}" if user else ""
        return f"✅ Reminder scheduled{user_text} at {time}: '{message}'"

    except Exception as e:
        return f"❌ Failed to schedule reminder: {str(e)}"


def add_to_shopping_list(items, quantity=None, requested_by=None):
    added_items = []
    for item in items:
        entry = {"item": item, "quantity": quantity, "requested_by": requested_by}
        shopping_list.append(entry)
        quantity_text = f" ({quantity})" if quantity else ""
        requester_text = f" (requested by {requested_by})" if requested_by else ""
        added_items.append(f"'{item}{quantity_text}'{requester_text}")
    
    return f"✅ Added to shopping list: {', '.join(added_items)}"

def remove_from_shopping_list(items):
    removed_items = []
    not_found_items = []

    for item in items:
        item_lower = item.lower()
        found = False
        for i, entry in enumerate(shopping_list):
            if entry['item'].lower() == item_lower:
                removed_items.append(shopping_list.pop(i)['item'])
                found = True
                break
        if not found:
            not_found_items.append(item)

    result = ""
    if removed_items:
        result += f"✅ Removed from shopping list: {', '.join(removed_items)}. "
    if not_found_items:
        result += f"❌ Items not found: {', '.join(not_found_items)}."

    return result.strip()

def get_shopping_list():
    if not shopping_list:
        return "📝 Shopping list is empty."
    
    items = []
    for i, entry in enumerate(shopping_list, 1):
        item = entry['item']
        quantity = f" ({entry['quantity']})" if entry.get('quantity') else ""
        requester = f" - {entry['requested_by']}" if entry.get('requested_by') else ""
        items.append(f"{i}. {item}{quantity}{requester}")
    
    return "📝 Current shopping list:\n" + "\n".join(items)

def clear_shopping_list():
    count = len(shopping_list)
    shopping_list.clear()
    return f"🗑️ Cleared shopping list ({count} items removed)"

def schedule_event(date, time, title, attendees=None):
    if attendees is None:
        attendees = members.split(", ")
    
    event = {"date": date, "time": time, "title": title, "attendees": attendees}
    events_list.append(event)
    attendee_text = f" for {', '.join(attendees)}" if len(attendees) < len(members.split(", ")) else ""
    return f"📅 Event '{title}' scheduled on {date} at {time}{attendee_text}"

def get_events():
    if not events_list:
        return "📅 No events scheduled."
    
    events = []
    for i, event in enumerate(events_list, 1):
        attendee_text = f" ({', '.join(event['attendees'])})" if event.get('attendees') else ""
        events.append(f"{i}. {event['title']} - {event['date']} at {event['time']}{attendee_text}")
    
    return "📅 Scheduled events:\n" + "\n".join(events)

def get_cleaning_schedule():
    return f"🧹 Cleaning schedule: {cleaning_schedule}"

# ---------------------------
# Flask routes
# ---------------------------
@app.route('/', methods=['GET'])
def home():
    return "C1 GroupMe Bot is running! 🤖"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        
        # Ignore bot messages to prevent loops
        if data.get('sender_type') == 'bot':
            return '', 200

        text = data.get('text', '').strip()
        if not text:
            return '', 200

        # Check for trigger words
        if any(trigger in text.lower() for trigger in TRIGGER_WORDS):
            name = data.get('name', 'Unknown')
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Get the persistent chat session
            chat = get_chat_session()
            
            # Create the message with user context
            user_message = f"[{timestamp}] {name}: {text}"
            
            try:
                # Send message to Gemini with system instruction
                response = chat.send_message(
                    user_message, 
                    config=types.GenerateContentConfig(
                        system_instruction=create_system_instruction(),
                        tools=[tools]
                    )
                )
                
                if not response.candidates:
                    send_groupme_message("Sorry, I encountered an error processing your request.")
                    return '', 200
                
                candidate = response.candidates[0].content.parts[0]
                reply_text = ""
                
                # Handle function call
                if hasattr(candidate, 'function_call') and candidate.function_call:
                    function_name = candidate.function_call.name
                    function_args = dict(candidate.function_call.args)
                    
                    # Execute the function
                    function_result = call_backend_function(function_name, function_args)
                    
                    # Send function result back to get a natural language response
                    function_response = types.FunctionResponse(
                        name=function_name,
                        response={"result": function_result}
                    )
                    
                    # Send the function result back to the chat
                    follow_up_response = chat.send_message(
                        types.Part(function_response=function_response),
                        config=types.GenerateContentConfig(
                            system_instruction=create_system_instruction(),
                            tools=[tools]
                        )
                    )
                    
                    if follow_up_response.candidates and follow_up_response.candidates[0].content.parts:
                        reply_text = follow_up_response.candidates[0].content.parts[0].text
                    else:
                        reply_text = function_result  # Fallback to function result
                
                # Handle regular text response
                elif hasattr(candidate, 'text') and candidate.text:
                    reply_text = candidate.text
                else:
                    reply_text = "I processed your request but couldn't generate a response."
                
                # Send the response
                if reply_text:
                    send_groupme_message(reply_text)
                else:
                    send_groupme_message("I processed your request successfully.")
                    
            except Exception as e:
                print(f"{str(e)}")
                send_groupme_message("Sorry, I encountered an error processing your request.")

        return '', 200

    except Exception as e:
        print(f"Error in webhook: {e}")
        return '', 500

@app.route('/health', methods=['GET'])
def health():
    return {'status': 'healthy', 'timestamp': current_eastern_time()}, 200

@app.route('/reset-chat', methods=['POST'])
def reset_chat():
    """Reset the chat session (useful for debugging)"""
    global chat_session
    chat_session = None
    return {'status': 'Chat session reset'}, 200
