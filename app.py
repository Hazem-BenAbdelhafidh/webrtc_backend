import logging
from aiohttp import web
import socketio

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
    caller_number = sid_to_number.get(sid, "Unknown")
    print(f"Offer from {caller_number} to {target_number}")
    
    if target_number in users:
        target_sid = users[target_number]
        data["caller"] = caller_number
        await sio.emit("incoming-call", data, to=target_sid)
    else:
        print(f"Target {target_number} not found.")
        await sio.emit("call-failed", {"reason": "User offline"}, to=sid)

@sio.on("answer")
async def answer(sid, data):
    target_number = str(data.get("destination"))
    print(f"Answer for {target_number} from {sid_to_number.get(sid)}")
    
    if target_number in users:
        target_sid = users[target_number]
        await sio.emit("answer", data, to=target_sid)

@sio.on("ice-candidate")
async def ice_candidate(sid, data):
    target_number = str(data.get("destination"))
    if target_number in users:
        target_sid = users[target_number]
        await sio.emit("ice-candidate", data["candidate"], to=target_sid)

@sio.on("end-call")
async def end_call(sid, data):
    target_number = str(data.get("destination"))
    print(f"End call for {target_number} from {sid_to_number.get(sid)}")
    if target_number in users:
        target_sid = users[target_number]
        await sio.emit("end-call", {"caller": sid_to_number.get(sid)}, to=target_sid)

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
