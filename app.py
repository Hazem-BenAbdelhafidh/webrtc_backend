import logging
import json
import asyncio
from aiohttp import web
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from aiortc.contrib.media import MediaRelay, MediaRecorder

relay = MediaRelay()

class CallManager:
    def __init__(self):
        self.calls = {} # type: dict
        self.sid_to_call = {} # type: dict

    def get_or_create_call(self, caller_num: str, callee_num: str):
        # Ensure consistent call_id regardless of who calls whom
        nums = sorted([caller_num, callee_num])
        call_id = f"{nums[0]}-{nums[1]}"
        if call_id not in self.calls:
            self.calls[call_id] = {
                "caller_num": caller_num,
                "callee_num": callee_num,
                "pcs": {}, # sid -> pc
                "senders": {}, # sid -> sender
                "tracks": {}, # number -> track
                "recorder": None,
                "recording_started": False
            }
        return call_id, self.calls[call_id]

    async def close_call(self, call_id: str):
        if call_id in self.calls:
            call = self.calls.pop(call_id)

            # Stop recorder if active
            if call["recorder"]:
                try:
                    await call["recorder"].stop()
                    print(f"Recording for {call_id} saved.")
                except Exception as e:
                    print(f"Error stopping recorder: {e}")

            for sid, pc in list(call["pcs"].items()):
                await pc.close()
                self.sid_to_call.pop(sid, None)
            print(f"Call {call_id} closed.")

call_manager = CallManager()

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
    pc = RTCPeerConnection()
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
        subscribed_track = relay.subscribe(track)
        call["tracks"][caller_number] = subscribed_track

        # Add track to recorder
        if not call["recorder"]:
            call["recorder"] = MediaRecorder(f"recordings/call_{call_id}.wav")
        call["recorder"].addTrack(relay.subscribe(track))
        if not call["recording_started"]:
            await call["recorder"].start()
            call["recording_started"] = True

        # Forward caller's track to all other peers via their existing senders
        for peer_sid, peer_sender in list(call["senders"].items()):
            if peer_sid != sid:
                print(f"Replacing track for peer {sid_to_number.get(peer_sid)} with caller's track")
                await peer_sender.replaceTrack(subscribed_track)
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
    await sio.emit("answer", {
        "sdp": pc.localDescription.sdp,
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
    pc = RTCPeerConnection()
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
        subscribed_track = relay.subscribe(track)
        call["tracks"][callee_number] = subscribed_track

        # Add track to recorder
        if not call["recorder"]:
            call["recorder"] = MediaRecorder(f"recordings/call_{call_id}.wav")
        call["recorder"].addTrack(relay.subscribe(track))
        if not call["recording_started"]:
            await call["recorder"].start()
            call["recording_started"] = True

        # Forward callee's track to all other peers via their existing senders
        for peer_sid, peer_sender in list(call["senders"].items()):
            if peer_sid != sid:
                print(f"Replacing track for peer {sid_to_number.get(peer_sid)} with callee's track")
                await peer_sender.replaceTrack(subscribed_track)

    # Create offer for callee
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    print(f"Generated offer for {callee_number}: {pc.localDescription.sdp[:100]}...")

    await sio.emit("offer", {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
        "caller": caller_number
    }, to=sid)

    if caller_number in users:
        await sio.emit("call-accepted", {"callee": callee_number}, to=users[caller_number])

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

app.router.add_post('/upload-recording', upload_recording)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, port=8000)
