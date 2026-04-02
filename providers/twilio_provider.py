import os
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from providers.base import BaseCallingProvider

class TwilioProvider(BaseCallingProvider):
    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.api_key = os.getenv("TWILIO_API_KEY")
        self.api_secret = os.getenv("TWILIO_API_SECRET")
        self.twiml_app_sid = os.getenv("TWILIO_TWIML_APP_SID")
        self._caller_id = os.getenv("TWILIO_PHONE_NUMBER")
        self.push_credential_sid = os.getenv("TWILIO_PUSH_CREDENTIAL_SID")

        # Twilio Client for REST API
        self.client = Client(self.api_key, self.api_secret, self.account_sid)

    def generate_token(self, identity: str) -> str:
        if not self.twiml_app_sid:
            print("[Twilio] TWILIO_TWIML_APP_SID is not defined in .env")

        print(f"[Twilio] Generating token for identity: {identity}")
        # Twilio API Key must be the issuer (signing_key_sid)
        # Twilio Account SID must be the subject
        token = AccessToken(
            account_sid=self.account_sid,
            signing_key_sid=self.api_key,
            secret=self.api_secret,
            identity=identity
        )

        print(f"Token is : {token}")

        voice_grant = VoiceGrant(
            outgoing_application_sid=self.twiml_app_sid,
            push_credential_sid=self.push_credential_sid,
            incoming_allow=True
        )

        token.add_grant(voice_grant)
        jwt_token = token.to_jwt()
        if isinstance(jwt_token, bytes):
            return jwt_token.decode("utf-8")
        return jwt_token

    def generate_voice_response(self, room_name: str, should_record: bool, app_base_url: str = "") -> str:
        """
        Creates TwiML for a participant to join a conference room.
        We also start an audio stream for transcription.
        """
        print(f"[Twilio] Joining conference room: {room_name}, Record: {should_record}")
        voice_response = VoiceResponse()

        # Start the media stream for transcription
        if app_base_url:
            # Convert https://... to wss://...
            wss_url = app_base_url.replace("https://", "wss://").replace("http://", "ws://")
            stream_url = f"{wss_url}/stream"
            start = voice_response.start()
            start.stream(url=stream_url)
            print(f"[Twilio] Starting audio stream to: {stream_url}")

        dial = voice_response.dial()
        # Conference name is the room_name.
        status_url = f"{app_base_url}/voice/status" if app_base_url else ""
        
        dial.conference(
            room_name,
            start_conference_on_enter=True,
            end_conference_on_exit=False,
            beep="false",
            wait_url="",
            jitter_buffer_delay="0",
            record="record-from-start" if should_record else "do-not-record",
            status_callback=status_url,
            status_callback_event="start end join leave"
        )

        return str(voice_response)

    def initiate_outbound_call(self, to_number: str, room_name: str, should_record: bool, callback_url: str):
        """
        Makes an outbound call to a phone number and pulls them into a conference room.
        """
        print(f"[Twilio] Initiating outbound call to {to_number} to join room {room_name}")

        # We need to provide a URL that Twilio will request when the call is answered.
        # This URL must return the TwiML to join the conference.
        # We'll pass the room_name and record params to the callback.

        # If callback_url is something like https://example.com/voice
        # We'll append the necessary parameters.
        separator = "&" if "?" in callback_url else "?"
        final_url = f"{callback_url}{separator}room={room_name}&record={'true' if should_record else 'false'}"

        try:
            call = self.client.calls.create(
                to=to_number,
                from_=self._caller_id,
                url=final_url
            )
            print(f"[Twilio] Outbound call SID: {call.sid}")
            return call.sid
        except Exception as e:
            print(f"[Twilio] Error initiating outbound call: {e}")
            raise e

    def terminate_call(self, call_sid: str):
        """Terminated a Twilio call."""
        try:
            self.client.calls(call_sid).update(status="completed")
            print(f"[Twilio] Terminated call leg: {call_sid}")
        except Exception as e:
            print(f"[Twilio] Error terminating call {call_sid}: {e}")

    @property
    def content_type(self) -> str:
        return "text/xml"

    @property
    def caller_id(self) -> str:
        return self._caller_id

    @property
    def name(self) -> str:
        return "twilio"
