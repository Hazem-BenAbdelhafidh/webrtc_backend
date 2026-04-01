import os
import telnyx
from providers.base import BaseCallingProvider

class TelnyxProvider(BaseCallingProvider):
    def __init__(self):
        self.api_key = os.getenv("TELNYX_API_KEY")
        self.connection_id = os.getenv("TELNYX_CONNECTION_ID")
        self._caller_id = os.getenv("TELNYX_NUMBER")

        # Validate required Telnyx credentials
        missing_vars = []
        if not self.api_key: missing_vars.append("TELNYX_API_KEY")
        if not self.connection_id: missing_vars.append("TELNYX_CONNECTION_ID")
        if not self._caller_id: missing_vars.append("TELNYX_NUMBER")

        if missing_vars:
            err_msg = f"Missing Telnyx configuration environment variables: {', '.join(missing_vars)}"
            print(f"[Telnyx] {err_msg}")
            raise ValueError(err_msg)

        self.client = None
        if self.api_key:
            self.client = telnyx.Telnyx(api_key=self.api_key)

    def generate_token(self, identity: str) -> str:
        """
        Generates a Telnyx WebRTC JWT token.
        Fallbacks to direct REST API if SDK's telephony_credentials fails (common for numeric IDs).
        """
        if not self.connection_id:
            print("[Telnyx] TELNYX_CONNECTION_ID is not defined in .env")
            raise ValueError("TELNYX_CONNECTION_ID is missing.")

        if not self.client:
            raise ValueError("Telnyx client not initialized. Check TELNYX_API_KEY.")

        print(f"[Telnyx] Generating JWT for ID: {self.connection_id}")

        # Strategy 1: Attempt SDK (Stainless-generated)
        try:
            # Note: This often returns 400 if the ID is a numeric TeXML/Connection ID
            response = self.client.telephony_credentials.create_token(id=self.connection_id)
            # The SDK might return a nested object or a string depending on version
            token = getattr(response, "data", {}).get("token") or str(response)

            if token and "ey" in token: # Simple JWT check
                return token
        except Exception as e:
            print(f"[Telnyx] SDK token creation failed, attempting REST fallback: {e}")

        # Strategy 2: Create an On-demand Credential then generate token
        try:
            # For numeric IDs (TeXML Apps/SIP Connections), we create a temporary
            # telephony credential, then use its UUID to get a token.
            print(f"[Telnyx] Creating on-demand credential for Connection ID: {self.connection_id}")
            response = self.client.telephony_credentials.create(
                connection_id=str(self.connection_id)
            )

            # The token might be in the response directly, or we use the new ID
            token = getattr(response, "token", None) or getattr(response.data, "token", None)

            if not token:
                new_id = getattr(response, "id", None) or getattr(response.data, "id", None)
                if new_id:
                    print(f"[Telnyx] Credential created (ID: {new_id}), generating token...")
                    token = self.client.telephony_credentials.create_token(id=new_id)

            if token:
                # Ensure we return the raw string and strip any whitespace
                final_token = str(getattr(token, "data", {}).get("token") or token).strip()
                print(f"[Telnyx] Successfully generated JWT.")
                return final_token
            else:
                print(f"[Telnyx] Failed to obtain token from on-demand credential: {response}")
        except Exception as e:
            print(f"[Telnyx] All token generation strategies failed: {e}")
            raise e

    def generate_voice_response(self, room_name: str, should_record: bool, app_base_url: str = "") -> str:
        """
        Generates Telnyx TeXML for a participant to join a conference room.
        We also start an audio stream for transcription.
        """
        print(f"[Telnyx] Joining conference room: {room_name}, Record: {should_record}")

        record_attr = ' record="record-from-answer"' if should_record else ""

        # Start the media stream for transcription
        stream_xml = ""
        if app_base_url:
            wss_url = app_base_url.replace("https://", "wss://").replace("http://", "ws://")
            stream_url = f"{wss_url}/stream"
            stream_xml = f'    <Start><Stream url="{stream_url}"/></Start>\n'
            print(f"[Telnyx] Starting audio stream to: {stream_url}")

        texml = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Response>\n'
            f'{stream_xml}'
            f'    <Dial>\n'
            f'        <Conference startConferenceOnEnter="true" endConferenceOnExit="true"{record_attr}>\n'
            f'            {room_name}\n'
            f'        </Conference>\n'
            f'    </Dial>\n'
            f'</Response>'
        )

        return texml

    def initiate_outbound_call(self, to_number: str, room_name: str, should_record: bool, callback_url: str):
        """
        Makes an outbound call using Telnyx Call Control and pulls them into a conference room.
        """
        print(f"[Telnyx] Initiating outbound call to {to_number} to join room {room_name}")

        # If callback_url is something like https://example.com/voice
        # We'll append the necessary parameters.
        separator = "&" if "?" in callback_url else "?"
        final_url = f"{callback_url}{separator}room={room_name}&record={'true' if should_record else 'false'}"

        try:
            # Use the new client-based approach for SDK 4.90.0
            call = self.client.calls.create(
                to=to_number,
                from_=self._caller_id,
                connection_id=self.connection_id,
                connection_url=final_url
            )
            # The returned object structure might have changed slightly
            call_control_id = getattr(call.data, 'call_control_id', None) or getattr(call, 'call_control_id', None)
            print(f"[Telnyx] Outbound call control ID: {call_control_id}")
            return call_control_id
        except Exception as e:
            print(f"[Telnyx] Error initiating outbound call: {e}")
            raise e

    def terminate_call(self, call_sid: str):
        """Terminates a Telnyx call leg."""
        try:
            # For Telnyx, call_sid is the call_control_id
            self.client.calls.actions.hangup(id=call_sid)
            print(f"[Telnyx] Terminated call leg: {call_sid}")
        except Exception as e:
            print(f"[Telnyx] Error terminating call {call_sid}: {e}")

    @property
    def content_type(self) -> str:
        return "text/xml"

    @property
    def caller_id(self) -> str:
        return self._caller_id

    @property
    def name(self) -> str:
        return "telnyx"
