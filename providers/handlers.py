import os
import re
from aiohttp import web
from dotenv import load_dotenv
from providers.factory import get_provider

# Load environment variables
load_dotenv()

from providers.factory import get_provider
from manager import call_manager

# Initialize provider based on environment
provider = get_provider()

def is_phone_number(to_number: str) -> bool:
    """
    Simple check if the 'To' parameter is a phone number.
    Matches strings that start with '+' or only contain digits.
    """
    if not to_number:
        return False
    # Remove any spaces, dashes, or parentheses
    clean_number = re.sub(r'[\s\-\(\)]', '', to_number)
    return clean_number.startswith('+') or clean_number.isdigit()

async def twilio_token_handler(request):
    """
    Handles token generation requests.
    Query Params: identity
    """
    identity = request.query.get("identity", "hazem")
    try:
        token = provider.generate_token(identity)
        return web.json_response({
            "token": token,
            "provider": provider.name,
            "callerId": provider.caller_id
        })
    except Exception as e:
        print(f"[Error] Failed to generate token: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def twilio_voice_handler(request):
    """
    Handles incoming voice calls from the provider.
    This also handles the callback logic for outbound conference invitations.
    """
    print("[Voice] Webhook triggered")
    
    # APP_BASE_URL is required to build the callback URL for outbound calls
    app_base_url = os.getenv("APP_BASE_URL", "").rstrip('/')
    
    # Get parameters from POST form data or Query string
    try:
        params = await request.post()
    except Exception:
        params = {}
    query = request.query
    
    # 'To' might be a phone number or a room name
    to_number = (
        params.get("To")
        or query.get("To")
        or params.get("to")
        or query.get("to")
    )

    should_record = (
        params.get("record") == "true"
        or query.get("record") == "true"
    )

    # Check if we are being called back (outbound leg)
    room_name = query.get("room")
    
    if room_name:
        # This is an outbound call callback, just join the room
        print(f"[Voice] Joining room '{room_name}' (outbound leg callback)")
        response_content = provider.generate_voice_response(room_name, should_record, app_base_url)
        return web.Response(text=response_content, content_type=provider.content_type)

    if not to_number:
        print("[Voice] No destination provided!")
        return web.Response(text="No destination number provided.", status=400)

    # If 'To' is a phone number, we create a room and invite the phone number
    if is_phone_number(to_number):
        # Generate a room name (we can use the phone number itself, or a hash)
        # Using the phone number as the room name for simplicity.
        room_name = re.sub(r'[^a-zA-Z0-9]', '', to_number) 
        
        callback_url = f"{app_base_url}/voice"
        if not app_base_url:
            print("[Warning] APP_BASE_URL not set! Outbound calls will fail.")
        
        # Get the caller's number to track the leg
        from_number = params.get("From") or query.get("From") or "unknown"
        
        try:
            # Invite the phone number to the room
            leg_id = provider.initiate_outbound_call(to_number, room_name, should_record, callback_url)
            
            # Track this leg in the manager
            if leg_id:
                call_manager.add_leg_id(from_number, to_number, leg_id)
            
            # Put the original caller (WebRTC) into the same room
            response_content = provider.generate_voice_response(room_name, should_record, app_base_url)
            return web.Response(text=response_content, content_type=provider.content_type)
        except Exception as e:
            print(f"[Error] Failed to initiate outbound call: {e}")
            return web.Response(text="Error initiating outbound call.", status=500)
    else:
        # 'To' is already a room name
        print(f"[Voice] Joining room '{to_number}'")
        response_content = provider.generate_voice_response(to_number, should_record, app_base_url)
        return web.Response(text=response_content, content_type=provider.content_type)

async def home_handler(request):
    """
    Simple heartbeat/home route.
    """
    return web.Response(text="Calling Backend is running (Conference with Phone Support)!")
