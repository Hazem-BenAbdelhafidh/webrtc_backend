import asyncio

class CallManager:
    def __init__(self):
        self.calls = {} # room_name -> call_data
        self.sid_to_call = {} # sid -> room_name
        self.provider = None # Set by app.py

    def get_or_create_call(self, room_name: str, participants_info: dict = None):
        """
        Retrieves or creates a conference call session by room name.
        """
        if room_name not in self.calls:
            print(f"[Manager] Creating new room: {room_name}")
            self.calls[room_name] = {
                "room_name": room_name,
                "pcs": {}, # sid -> pc
                "senders": {}, # sid -> sender
                "tracks": {}, # participant_identity -> track
                "provider_leg_ids": [], # List of SIDs (from PSTN) to hang up
                "recorder": None,
                "recording_started": False,
                "start_time": asyncio.get_event_loop().time()
            }
        
        return room_name, self.calls[room_name]

    def add_leg_id(self, room_name: str, leg_id: str):
        """Adds a PSTN leg ID to a specific room."""
        if room_name in self.calls:
            if leg_id not in self.calls[room_name]["provider_leg_ids"]:
                self.calls[room_name]["provider_leg_ids"].append(leg_id)
                print(f"[Manager] Added leg ID {leg_id} to room {room_name}")

    def get_active_calls(self):
        """Returns a list of all active rooms with their participant count."""
        active = []
        for room_name, data in self.calls.items():
            active.append({
                "room_name": room_name,
                "participant_count": len(data["pcs"]) + len(data["provider_leg_ids"]),
                "duration": int(asyncio.get_event_loop().time() - data["start_time"])
            })
        return active

    async def close_call(self, room_name: str):
        """Terminates all legs and closes all PCs in a room."""
        if room_name in self.calls:
            call = self.calls.pop(room_name)

            # 1. Close WebRTC PeerConnections
            for sid, pc in list(call["pcs"].items()):
                try:
                    await pc.close()
                except Exception: pass
                self.sid_to_call.pop(sid, None)
            
            # 2. Terminate PSTN legs via provider
            if self.provider:
                for leg_id in call["provider_leg_ids"]:
                    try:
                        self.provider.terminate_call(leg_id)
                    except Exception as e:
                        print(f"[Manager] Error terminating leg {leg_id}: {e}")

            print(f"Room {room_name} closed (all participants removed).")

call_manager = CallManager()
