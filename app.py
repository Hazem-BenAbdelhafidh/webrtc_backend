import os
import asyncio
from aiohttp import web
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from aiortc.contrib.media import MediaRelay

# Single relay instance — buffered=False keeps forwarding latency minimal
relay = MediaRelay()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pc() -> RTCPeerConnection:
    """Create a PeerConnection with no ICE servers on the server side.

    The server has a known public IP so host candidates are sufficient and
    skipping STUN removes one full round-trip of gathering delay.
    Add a TURN server here only if your server is behind NAT.
    """
    return RTCPeerConnection(
        configuration=RTCConfiguration(
            iceServers=[]
            # iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
        )
    )


# ---------------------------------------------------------------------------
# Call Manager
# ---------------------------------------------------------------------------

class CallManager:
    def __init__(self):
        self.calls: dict = {}
        self.sid_to_call: dict = {}

    def get_or_create_call(self, caller_num: str, callee_num: str):
        nums = sorted([caller_num, callee_num])
        call_id = f"{nums[0]}-{nums[1]}"
        if call_id not in self.calls:
            self.calls[call_id] = {
                "caller_num": caller_num,
                "callee_num": callee_num,
                "pcs": {},      # sid    -> RTCPeerConnection
                "senders": {},  # sid    -> RTCRtpSender
                "tracks": {},   # number -> subscribed MediaStreamTrack
            }
        return call_id, self.calls[call_id]

    async def close_call(self, call_id: str):
        if call_id not in self.calls:
            return
        call = self.calls.pop(call_id)
        for sid, pc in list(call["pcs"].items()):
            await pc.close()
            self.sid_to_call.pop(sid, None)
        print(f"[call] {call_id} closed.")


call_manager = CallManager()

# ---------------------------------------------------------------------------
# Socket.IO / aiohttp app
# ---------------------------------------------------------------------------

sio = socketio.AsyncServer(
    async_mode="aiohttp",
    cors_allowed_origins="*",
    max_http_buffer_size=10_000_000,
)
app = web.Application()
sio.attach(app)

users: dict = {}         # number -> sid
sid_to_number: dict = {} # sid    -> number


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

@sio.event
async def connect(sid, environ):
    print(f"[ws] connected  {sid}")


@sio.event
async def disconnect(sid):
    print(f"[ws] disconnected {sid}")
    if sid in call_manager.sid_to_call:
        call_id = call_manager.sid_to_call[sid]
        await call_manager.close_call(call_id)
    if sid in sid_to_number:
        number = sid_to_number.pop(sid)
        users.pop(number, None)
        print(f"[reg] {number} unregistered.")


@sio.on("register")
async def register(sid, data):
    number = str(data.get("number", ""))
    if not number:
        return {"error": "number required"}
    users[number] = sid
    sid_to_number[sid] = number
    print(f"[reg] {number} → {sid}")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Shared helper: wire up a fresh PeerConnection for one participant
# ---------------------------------------------------------------------------

async def _setup_pc_for_peer(
    sid: str,
    peer_number: str,
    other_number: str,
    call_id: str,
    call: dict,
    initial_track=None,
):
    """
    Create and fully configure a PeerConnection for `peer_number`.

    Latency optimisations:
      - Transceiver added before offer/answer so SDP already declares the
        send direction — no renegotiation needed when the track arrives.
      - If the other peer's track already exists it is loaded into the sender
        immediately so the very first SDP exchange carries live audio.
      - Tracks are forwarded with buffered=False (no relay jitter buffer).
    """
    pc = _make_pc()
    call["pcs"][sid] = pc
    call_manager.sid_to_call[sid] = call_id

    transceiver = pc.addTransceiver("audio", direction="sendrecv")
    call["senders"][sid] = transceiver.sender

    # Pre-load the remote peer's track if already available
    if initial_track is not None:
        await transceiver.sender.replaceTrack(initial_track)

    # ---- ICE candidates ------------------------------------------------
    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            await sio.emit(
                "ice-candidate",
                {
                    "candidate": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    },
                    "destination": peer_number,
                },
                to=sid,
            )

    # ---- Incoming track from this peer ---------------------------------
    @pc.on("track")
    async def on_track(track):
        print(f"[track] received from {peer_number}")

        # buffered=False → frames forwarded immediately, no jitter queue
        subscribed = relay.subscribe(track, buffered=False)
        call["tracks"][peer_number] = subscribed

        # Push this peer's track to every other peer's sender right away
        for peer_sid, peer_sender in list(call["senders"].items()):
            if peer_sid != sid:
                await peer_sender.replaceTrack(subscribed)
                print(f"[track] forwarded to {sid_to_number.get(peer_sid)}")

    return pc


# ---------------------------------------------------------------------------
# offer  (caller -> server)
# ---------------------------------------------------------------------------

@sio.on("offer")
async def offer(sid, data):
    caller_number = sid_to_number.get(sid)
    target_number = str(data.get("destination", ""))
    sdp = data.get("sdp")

    if not caller_number or not target_number:
        return

    print(f"[offer] {caller_number} → {target_number}")

    call_id, call = call_manager.get_or_create_call(caller_number, target_number)

    callee_track = call["tracks"].get(target_number)
    pc = await _setup_pc_for_peer(
        sid, caller_number, target_number, call_id, call,
        initial_track=callee_track,
    )

    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    await sio.emit(
        "answer",
        {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "destination": caller_number,
        },
        to=sid,
    )

    # Notify callee if online
    if target_number in users:
        target_sid = users[target_number]
        if target_sid not in call["pcs"]:
            await sio.emit("incoming-call", {"caller": caller_number}, to=target_sid)
    else:
        print(f"[offer] target {target_number} offline")
        await sio.emit("call-failed", {"reason": "User offline"}, to=sid)


# ---------------------------------------------------------------------------
# answer  (callee -> server, after server sent offer to callee)
# ---------------------------------------------------------------------------

@sio.on("answer")
async def answer(sid, data):
    callee_number = sid_to_number.get(sid)
    caller_number = str(data.get("destination", ""))
    sdp = data.get("sdp")

    print(f"[answer] {callee_number} for {caller_number}")

    call_id = call_manager.sid_to_call.get(sid)
    if not call_id:
        return
    call = call_manager.calls.get(call_id)
    if not call:
        return
    pc = call["pcs"].get(sid)
    if pc:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))


# ---------------------------------------------------------------------------
# ice-candidate
# ---------------------------------------------------------------------------

@sio.on("ice-candidate")
async def ice_candidate(sid, data):
    if sid not in call_manager.sid_to_call:
        return
    call_id = call_manager.sid_to_call[sid]
    call = call_manager.calls.get(call_id)
    pc = call["pcs"].get(sid) if call else None
    if not pc or not data:
        return

    candidate_dict = data.get("candidate") if isinstance(data, dict) else data
    if not candidate_dict:
        return
    cand_str = candidate_dict.get("candidate")
    if cand_str:
        try:
            candidate = candidate_from_sdp(cand_str)
            candidate.sdpMid = candidate_dict.get("sdpMid")
            candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
            await pc.addIceCandidate(candidate)
        except Exception as e:
            print(f"[ice] error: {e}")


# ---------------------------------------------------------------------------
# accept-call  (callee accepts -> server creates callee's PC and sends offer)
# ---------------------------------------------------------------------------

@sio.on("accept-call")
async def accept_call(sid, data):
    caller_number = str(data.get("caller", ""))
    callee_number = sid_to_number.get(sid)

    print(f"[accept] {callee_number} accepted from {caller_number}")

    call_id, call = call_manager.get_or_create_call(caller_number, callee_number)

    caller_track = call["tracks"].get(caller_number)
    pc = await _setup_pc_for_peer(
        sid, callee_number, caller_number, call_id, call,
        initial_track=caller_track,
    )

    offer_desc = await pc.createOffer()
    await pc.setLocalDescription(offer_desc)

    await sio.emit(
        "offer",
        {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "caller": caller_number,
        },
        to=sid,
    )

    if caller_number in users:
        await sio.emit("call-accepted", {"callee": callee_number}, to=users[caller_number])


# ---------------------------------------------------------------------------
# end-call
# ---------------------------------------------------------------------------

@sio.on("end-call")
async def end_call(sid, data):
    caller_number = sid_to_number.get(sid)
    target_number = str(data.get("destination", ""))
    call_id = call_manager.sid_to_call.get(sid)
    if call_id:
        await call_manager.close_call(call_id)
    if target_number in users:
        await sio.emit("end-call", {"caller": caller_number}, to=users[target_number])


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    web.run_app(app, port=port)