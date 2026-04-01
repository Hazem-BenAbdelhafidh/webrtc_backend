import asyncio

class CallManager:
    def __init__(self):
        self.calls = {} # type: dict
        self.sid_to_call = {} # type: dict
        self.provider = None # Set by app.py

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
                "provider_leg_ids": [], # List of SIDs to hang up
                "recorder": None,
                "recording_started": False
            }
        return call_id, self.calls[call_id]

    def add_leg_id(self, caller_num: str, callee_num: str, leg_id: str):
        _, call = self.get_or_create_call(caller_num, callee_num)
        if leg_id not in call["provider_leg_ids"]:
            call["provider_leg_ids"].append(leg_id)
            print(f"[Manager] Added leg ID {leg_id} to call between {caller_num} and {callee_num}")

    async def close_call(self, call_id: str):
        if call_id in self.calls:
            call = self.calls.pop(call_id)

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

            print(f"Call {call_id} closed (all legs terminated).")

call_manager = CallManager()
