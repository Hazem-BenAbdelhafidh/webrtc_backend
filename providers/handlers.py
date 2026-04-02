import os
import re
import asyncio
from aiohttp import web
from dotenv import load_dotenv
from providers.factory import get_provider

# Load environment variables
load_dotenv()

from providers.factory import get_provider
from manager import call_manager

# Initialize provider based on environment
provider = get_provider()

# Socket.IO instance (set by app.py)
sio = None


print("PORT:", os.environ.get("é"))


def is_phone_number(to_number: str) -> bool:
    """
    Simple check if the 'To' parameter is a phone number.
    Matches strings that start with '+' or only contain digits.
    """
    if not to_number:
        return False
    # Strip sip: prefix and domain if present
    to_number = to_number.replace('sip:', '').split('@')[0]
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
    print(f"[Voice] Provider: {provider.name}")

    # APP_BASE_URL is required to build the callback URL for outbound calls
    app_base_url = os.getenv("APP_BASE_URL", "").rstrip('/')

    # Log everything for debugging
    raw_body = await request.text()
    print(f"[Voice] Method: {request.method}, Content-Type: {request.content_type}")
    print(f"[Voice] Raw body: {raw_body[:1000]}")
    print(f"[Voice] Query string: {dict(request.query)}")

    # Get parameters from POST form data or Query string
    try:
        if 'application/json' in (request.content_type or ''):
            params = await request.json()
        elif request.method == 'GET':
            params = request.query
        else:
            # Re-parse the body since we already consumed it
            from urllib.parse import parse_qs
            parsed = parse_qs(raw_body)
            params = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
    except Exception as e:
        print(f"[Voice] Error parsing request body: {e}")
        params = {}
    query = request.query
    
    print(f"[Voice] Parsed params: {dict(params)}")
    print(f"[Voice] Parsed query: {dict(query)}")

    # 'To' might be a phone number or a room name
    # Also check Telnyx-specific parameters
    raw_to = (
        params.get("To")
        or query.get("To")
        or params.get("to")
        or query.get("to")
        or params.get("call_to")
        or query.get("call_to")
        or params.get("destination")
        or query.get("destination")
    )
    
    print(f"[Voice] Raw 'To' value: {raw_to}")
    
    # Strip SIP URI formatting if present (common in Telnyx WebRTC -> TeXML webhooks)
    to_number = raw_to
    if to_number and to_number.startswith('sip:'):
        to_number = to_number.replace('sip:', '').split('@')[0]
    
    print(f"[Voice] Cleaned 'To' value: {to_number}, is_phone: {is_phone_number(to_number)}")

    should_record = (
        params.get("record") == "true"
        or query.get("record") == "true"
    )

    # Check if we are being called back (outbound leg)
    room_name = query.get("room")

    if room_name:
        # This is an outbound call callback, ensure room exists and join
        print(f"[Voice] Joining room '{room_name}' (outbound leg callback)")
        
        # Ensure room exists (it was created when outbound call was initiated)
        if room_name not in call_manager.calls:
            print(f"[Voice] Room '{room_name}' not found, creating it now")
            call_manager.get_or_create_call(room_name)
            asyncio.create_task(broadcast_active_calls())
        
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
            # Generate a room name (we can use the phone number itself)
            room_name = re.sub(r'[^a-zA-Z0-9]', '', to_number)
            
            # Register the call room in manager
            call_manager.get_or_create_call(room_name)
            
            # Track the ORIGINAL caller (WebRTC participant) - this is the client making the call
            # We use a special prefix to distinguish from PSTN legs
            caller_marker = f"webrtc:{from_number}"
            if caller_marker not in call_manager.calls[room_name]["provider_leg_ids"]:
                call_manager.calls[room_name]["provider_leg_ids"].append(caller_marker)
                print(f"[Voice] Tracked WebRTC caller {caller_marker} in room {room_name}")
            
            # Invite the phone number to the room (outbound PSTN leg)
            leg_id = provider.initiate_outbound_call(to_number, room_name, should_record, callback_url)

            # Track this PSTN leg in the room
            if leg_id:
                call_manager.add_leg_id(room_name, leg_id)

            # Broadcast updated active calls
            asyncio.create_task(broadcast_active_calls())

            # Put the original caller (WebRTC) into the same room
            response_content = provider.generate_voice_response(room_name, should_record, app_base_url)
            return web.Response(text=response_content, content_type=provider.content_type)
        except Exception as e:
            print(f"[Error] Failed to initiate outbound call: {e}")
            return web.Response(text="Error initiating outbound call.", status=500)
    else:
        # 'To' is already a room name
        print(f"[Voice] Joining room '{to_number}'")
        
        # Register the call room in manager so it appears in live conferences
        call_manager.get_or_create_call(to_number)

        # Broadcast updated active calls
        asyncio.create_task(broadcast_active_calls())
        
        response_content = provider.generate_voice_response(to_number, should_record, app_base_url)
        return web.Response(text=response_content, content_type=provider.content_type)

async def active_calls_handler(request):
    """
    Returns a list of all active conference rooms.
    """
    try:
        active_calls = call_manager.get_active_calls()
        return web.json_response(active_calls)
    except Exception as e:
        print(f"[Error] Failed to get active calls: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def twilio_status_handler(request):
    """
    Handles Twilio conference status callbacks.
    - On 'conference-end': cleans up the CallManager.
    - On 'participant-leave': checks if only 1 participant remains, and if so, ends the conference.
    """
    try:
        if 'application/json' in (request.content_type or ''):
            data = await request.json()
        else:
            data = await request.post()
    except Exception:
        data = {}

    # For conferences, FriendlyName usually matches the room_name
    room_name = data.get("FriendlyName")
    status = data.get("StatusCallbackEvent")

    print(f"[Twilio Status] Event='{status}' Room='{room_name}'")

    if status == "conference-end" and room_name:
        import asyncio
        print(f"[Twilio] Conference '{room_name}' ended. Cleaning up call manager...")
        asyncio.create_task(call_manager.close_call(room_name))

    elif status == "participant-leave" and room_name:
        # End conference when no participants remain (changed from <=1 to <2)
        try:
            conferences = provider.client.conferences.list(
                friendly_name=room_name,
                status="in-progress"
            )
            if conferences:
                conf = conferences[0]
                participants = provider.client.conferences(conf.sid).participants.list()
                total_remaining = len(participants)
                print(f"[Twilio] Total participants remaining in '{room_name}': {total_remaining}")

                if total_remaining < 2:
                    print(f"[Twilio] Only {total_remaining} participant(s) left — ending conference '{room_name}'")
                    conf.update(status="completed")
                    asyncio.create_task(call_manager.close_call(room_name))
            else:
                print(f"[Twilio] No in-progress conference found for '{room_name}'")
                asyncio.create_task(call_manager.close_call(room_name))
        except Exception as e:
            print(f"[Twilio] Error checking/ending conference '{room_name}': {e}")

    return web.Response(text="ok", status=200)


async def telnyx_webhook_handler(request):
    """
    Handles Telnyx call control webhooks.
    - On 'call.completed': participant hung up - check if conference should end.
    - On 'call.suppressed': initial call setup (ignore).
    """
    import asyncio
    try:
        if 'application/json' in (request.content_type or ''):
            data = await request.json()
        else:
            data = await request.post()
    except Exception:
        data = {}

    event_type = data.get("event_type")
    payload = data.get("payload", {})
    call_control_id = payload.get("call_control_id")
    call_id = payload.get("call", {}).get("id")
    leg_id = call_control_id or call_id

    print(f"[Telnyx Webhook] Event='{event_type}' CallControlID='{call_control_id}' CallID='{call_id}'")

    if event_type == "call.completed" and leg_id:
        room_name = None

        # Find which room this leg belongs to
        for rname, calldata in list(call_manager.calls.items()):
            if leg_id in calldata.get("provider_leg_ids", []):
                room_name = rname
                # Remove this leg from the room
                if leg_id in calldata["provider_leg_ids"]:
                    calldata["provider_leg_ids"].remove(leg_id)
                print(f"[Telnyx] Removed leg {leg_id} from room '{room_name}'")
                break
            # Also check WebRTC participants
            for sid in list(calldata.get("pcs", {}).keys()):
                if call_manager.sid_to_call.get(sid) == rname:
                    pass  # WebRTC cleanup happens in disconnect handler

        if room_name:
            # Check if room should be closed (no PSTN legs and no WebRTC participants)
            leg_ids = call_manager.calls.get(room_name, {}).get("provider_leg_ids", [])
            # Count only actual PSTN legs (not webrtc: markers)
            remaining_pstn = len([lid for lid in leg_ids if not lid.startswith("webrtc:")])
            remaining_webrtc_markers = len([lid for lid in leg_ids if lid.startswith("webrtc:")])
            remaining_pcs = len(call_manager.calls.get(room_name, {}).get("pcs", {}))
            total_remaining = remaining_pstn + remaining_webrtc_markers + remaining_pcs

            print(f"[Telnyx] Room '{room_name}': {remaining_pstn} PSTN legs, {remaining_webrtc_markers} WebRTC markers, {remaining_pcs} PCs")

            if total_remaining < 2:
                print(f"[Telnyx] Only {total_remaining} participant(s) left in room '{room_name}' — closing")
                asyncio.create_task(call_manager.close_call(room_name))

        # Also broadcast updated active calls to connected clients
        asyncio.create_task(broadcast_active_calls())

    elif event_type == "call.initiated":
        print(f"[Telnyx] New call initiated")

    return web.Response(text="ok", status=200)


async def broadcast_active_calls():
    """Helper to broadcast active calls update to all connected clients."""
    try:
        if sio is not None:
            active_calls = call_manager.get_active_calls()
            await sio.emit("active_calls_updated", active_calls)
    except Exception as e:
        print(f"[Error] Failed to broadcast active calls: {e}")

async def home_handler(request):
    """
    Simple heartbeat/home route.
    """
    return web.Response(text="Calling Backend is running (Conference with Phone Support)!")

async def web_dashboard_handler(request):
    """
    Serves the simple web dashboard for viewing and joining calls.
    """
    try:
        with open("dashboard.html", "r") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Dashboard not found: {e}", status=404)
