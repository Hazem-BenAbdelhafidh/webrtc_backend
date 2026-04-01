import os
import asyncio
import json
import base64
from aiohttp import web
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
load_dotenv()

from providers.handlers import twilio_token_handler, twilio_voice_handler, home_handler, provider
from manager import call_manager

# Configure manager with the global provider
call_manager.provider = provider

sio = socketio.AsyncServer(
    async_mode='aiohttp',
    cors_allowed_origins='*',
    max_http_buffer_size=10000000 # 10MB
)
app = web.Application()
sio.attach(app)

# Mapping from phone number -> sid
users = {}
# Mapping from sid -> phone number
sid_to_number = {}

@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")
    if sid in call_manager.sid_to_call:
        call_id = call_manager.sid_to_call[sid]
        await call_manager.close_call(call_id)

    if sid in sid_to_number:
        number = sid_to_number.pop(sid)
        if number in users:
            users.pop(number, None)
        print(f"User {number} unregistered.")

@sio.on("register")
async def register(sid, data):
    number = str(data.get("number"))
    if not number:
        return {"error": "Number is required"}

    users[number] = sid
    sid_to_number[sid] = number
    print(f"User {number} registered with sid {sid}")
    return {"status": "ok"}

@sio.on("offer")
async def offer(sid, data):
    target_number = str(data.get("destination"))
    caller_number = sid_to_number.get(sid)
    sdp = data.get("sdp")

    if not caller_number or not target_number:
        return

    print(f"Offer from {caller_number} to {target_number}")

    call_id, call = call_manager.get_or_create_call(caller_number, target_number)
    call_manager.sid_to_call[sid] = call_id

    # Create PC for the caller on the server
    # Initialize PC and Receiver Transceiver
    pc = RTCPeerConnection(configuration=RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    ))
    call["pcs"][sid] = pc
    # Add a transceiver to receive and send audio
    transceiver = pc.addTransceiver("audio", direction="sendrecv")
    call["senders"][sid] = transceiver.sender

    # If the callee has already shared their track, add it to this sender
    callee_number = call["callee_num"]
    if callee_number in call["tracks"]:
        print(f"Adding callee's track to caller's PC initial bundle")
        transceiver.sender.replaceTrack(call["tracks"][callee_number])

    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            print(f"Server ICE candidate for {caller_number}")
            await sio.emit("ice-candidate", {
                "candidate": {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex
                },
                "destination": caller_number
            }, to=sid)

    @pc.on("track")
    async def on_track(track): # Change to async
        print(f"Received track from {caller_number}")
        call["tracks"][caller_number] = track

        # Forward caller's track to all other peers via their existing senders
        for peer_sid, peer_sender in list(call["senders"].items()):
            if peer_sid != sid:
                print(f"Replacing track for peer {sid_to_number.get(peer_sid)} with caller's track")
                peer_sender.replaceTrack(track)
                # No offer/answer needed for replaceTrack in aiortc usually,
                # but if the client hasn't connected yet, the initial offer will handle it.

    # Set remote description
    offer = RTCSessionDescription(sdp=sdp, type="offer")
    await pc.setRemoteDescription(offer)

    # Create answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    print(f"Generated answer for {caller_number}: {pc.localDescription.sdp[:100]}...")

    # Return answer to caller
    optimized_sdp = pc.localDescription.sdp
    await sio.emit("answer", {
        "sdp": optimized_sdp,
        "type": pc.localDescription.type,
        "destination": caller_number
    }, to=sid)

    # Notify callee if they are online
    if target_number in users:
        target_sid = users[target_number]
        # Check if we need to initiate a connection to the callee
        if target_sid not in call["pcs"]:
            print(f"Initiating connection to callee {target_number}")
            await sio.emit("incoming-call", {"caller": caller_number}, to=target_sid)
    else:
        print(f"Target {target_number} not found.")
        await sio.emit("call-failed", {"reason": "User offline"}, to=sid)

@sio.on("answer")
async def answer(sid, data):
    # This handler is called when the CALLEE sends their answer back to the server
    caller_number = str(data.get("destination")) # The server is the destination for the callee's answer
    callee_number = sid_to_number.get(sid)
    sdp = data.get("sdp")

    print(f"Answer from {callee_number} for {caller_number}")

    call_id = call_manager.sid_to_call.get(sid)
    if not call_id:
        return

    call = call_manager.calls.get(call_id)
    pc = call["pcs"].get(sid)

    if pc:
        answer = RTCSessionDescription(sdp=sdp, type="answer")
        await pc.setRemoteDescription(answer)

@sio.on("ice-candidate")
async def ice_candidate(sid, data):
    if sid not in call_manager.sid_to_call:
        return

    call_id = call_manager.sid_to_call[sid]
    call = call_manager.calls.get(call_id)
    pc = call["pcs"].get(sid)

    if pc and data:
        candidate_dict = data.get("candidate") if isinstance(data, dict) else data
        if not candidate_dict: return

        cand_str = candidate_dict.get("candidate")
        if cand_str:
            try:
                # aiortc uses a helper to parse candidate strings
                candidate = candidate_from_sdp(cand_str)
                candidate.sdpMid = candidate_dict.get("sdpMid")
                candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
                await pc.addIceCandidate(candidate)
                print(f"Added ICE candidate for {sid_to_number.get(sid)}")
            except Exception as e:
                print(f"Error adding ICE candidate: {e}")


@sio.on("accept-call")
async def accept_call(sid, data):
    # Callee accepts the call, server creates PC for callee and sends OFFER to callee
    caller_number = str(data.get("caller"))
    callee_number = sid_to_number.get(sid)

    print(f"Callee {callee_number} accepted call from {caller_number}")

    call_id, call = call_manager.get_or_create_call(caller_number, callee_number)
    call_manager.sid_to_call[sid] = call_id

    # Initialize Callee's PC
    pc = RTCPeerConnection(configuration=RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    ))
    call["pcs"][sid] = pc
    # Add a transceiver to receive and send audio
    transceiver = pc.addTransceiver("audio", direction="sendrecv")
    call["senders"][sid] = transceiver.sender

    # If the caller has already shared their track, add it to this sender
    if caller_number in call["tracks"]:
        print(f"Adding caller's track to callee's PC initial bundle")
        transceiver.sender.replaceTrack(call["tracks"][caller_number])

    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            await sio.emit("ice-candidate", {
                "candidate": {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex
                },
                "destination": callee_number
            }, to=sid)

    @pc.on("track")
    async def on_track(track): # Change to async
        print(f"Received track from {callee_number} (callee)")
        call["tracks"][callee_number] = track

        # Forward callee's track to all other peers via their existing senders
        for peer_sid, peer_sender in list(call["senders"].items()):
            if peer_sid != sid:
                print(f"Replacing track for peer {sid_to_number.get(peer_sid)} with callee's track")
                peer_sender.replaceTrack(track)

    # Create offer for callee
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    optimized_sdp = optimize_sdp(pc.localDescription.sdp)
    print(f"Generated optimized offer for {callee_number}")

    await sio.emit("offer", {
        "sdp": optimized_sdp,
        "type": pc.localDescription.type,
        "caller": caller_number
    }, to=sid)

    if caller_number in users:
        await sio.emit("call-accepted", {"callee": callee_number}, to=users[caller_number])

def optimize_sdp(sdp):
    """
    Mangle SDP to force Opus with absolute minimum latency settings.
    """
    print("Mangling backend SDP for lowest latency...")
    import re

    # 1. Force ptime:10
    if "a=ptime:" in sdp:
        sdp = re.sub(r"a=ptime:\d+", "a=ptime:10", sdp)
    else:
        sdp = sdp.replace("a=rtcp-mux", "a=rtcp-mux\r\na=ptime:10")

    # Isolate Opus Payload Type
    opus_pt = "111"
    rtpmap_match = re.search(r"a=rtpmap:(\d+)\s+opus/48000", sdp, re.IGNORECASE)
    if rtpmap_match:
        opus_pt = rtpmap_match.group(1)

    # Modify or add Opus fmtp parameters for maximum delay reduction
    def replace_fmtp(match):
        pt = match.group(1)
        params = match.group(2)
        if pt == opus_pt:
            if "minptime=" not in params: params += ";minptime=10"
            else: params = re.sub(r"minptime=\d+", "minptime=10", params)

            if "ptime=" not in params: params += ";ptime=10"
            else: params = re.sub(r"ptime=\d+", "ptime=10", params)

            if "useinbandfec=" not in params: params += ";useinbandfec=1"
            if "stereo=" not in params: params += ";stereo=0;sprop-stereo=0"

            # Use 32kbps Constant Bit Rate (CBR) to avoid encoder complexity & network jitter
            if "maxaveragebitrate=" not in params: params += ";maxaveragebitrate=32000"
            else: params = re.sub(r"maxaveragebitrate=\d+", "maxaveragebitrate=32000", params)

            if "cbr=" not in params: params += ";cbr=1"

            return f"a=fmtp:{pt}{params}"
        return match.group(0)

    fmtp_pattern = f"a=fmtp:{opus_pt}(.*)"
    if re.search(fmtp_pattern, sdp):
        sdp = re.sub(r"a=fmtp:(\d+)(.*)", replace_fmtp, sdp)
    else:
        # If no fmtp line exists for Opus, add one below its rtpmap
        rtpmap_line = rtpmap_match.group(0) if rtpmap_match else f"a=rtpmap:{opus_pt} opus/48000/2"
        optimized_fmtp = f"a=fmtp:{opus_pt} minptime=10;ptime=10;useinbandfec=1;stereo=0;sprop-stereo=0;maxaveragebitrate=32000;cbr=1"
        sdp = sdp.replace(rtpmap_line, f"{rtpmap_line}\r\n{optimized_fmtp}")

    return sdp

@sio.on("end-call")
async def end_call(sid, data):
    caller_number = sid_to_number.get(sid)
    target_number = str(data.get("destination", ""))
    call_id = call_manager.sid_to_call.get(sid)
    if call_id:
        await call_manager.close_call(call_id)  # closes PCs, cleans sid_to_call
    if target_number in users:
        await sio.emit("end-call", {"caller": caller_number}, to=users[target_number])

# 2. Guard renegotiate with a small delay + state check:
async def renegotiate(sid, pc):
    await asyncio.sleep(0.1)  # let ICE settle
    if pc.signalingState == "stable":
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await sio.emit("offer", {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "destination": sid_to_number.get(sid)
        }, to=sid)

async def upload_recording(request):
    reader = await request.multipart()
    field = await reader.next()
    if field.name == 'file':
        filename = field.filename
        filepath = f"recordings/{filename}"
        size = 0
        with open(filepath, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
        print(f"Saved recording: {filepath} ({size} bytes)")
        return web.json_response({'status': 'ok', 'filename': filename})
    return web.json_response({'error': 'No file found'})

async def log_metrics(request):
    try:
        data = await request.json()
        # Extract useful metrics for easy reading
        rtt = data.get('rtt', 'N/A')
        jitter = data.get('jitter', 'N/A')
        loss = data.get('loss', 0)
        mos = data.get('mos', 'N/A')
        print(f"[Metrics] RTT: {rtt}ms | Jitter: {jitter}ms | Loss: {loss} | MOS: {mos}")
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def stream_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    print(f"[Stream] WebSocket connection established")

    call_sid = None

    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
                event = data.get("event")

                if event == "start":
                    call_sid = data.get('start', {}).get('callSid') or data.get('start', {}).get('streamSid')
                    print(f"[Stream] Audio stream started for Call SID: {call_sid}")
                elif event == "media":
                    # Here is where the raw audio bytes are (in base64)
                    payload = data.get('media', {}).get('payload')
                    chunk = base64.b64decode(payload)
                    print("audio chunk hazem : ", chunk)
                    # [TRANSCRIPTION LOGIC GOES HERE]
                    pass
                elif event == "stop":
                    print(f"[Stream] Audio stream stopped for Call SID: {call_sid}")
            except Exception as e:
                print(f"[Stream] Error parsing message: {e}")
        elif msg.type == web.WSMsgType.ERROR:
            print(f"[Stream] WebSocket connection closed with error {ws.exception()}")

    print(f"[Stream] WebSocket connection closed")
    return ws

# Register Routes
app.router.add_get('/', home_handler)
app.router.add_get('/token', twilio_token_handler)
app.router.add_post('/voice', twilio_voice_handler)
app.router.add_post('/upload-recording', upload_recording)
app.router.add_post('/log-metrics', log_metrics)
app.router.add_get('/stream', stream_handler)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    web.run_app(app, port=port)
