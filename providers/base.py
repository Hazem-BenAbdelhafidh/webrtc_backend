from abc import ABC, abstractmethod

class BaseCallingProvider(ABC):
    @abstractmethod
    def generate_token(self, identity: str):
        """Generates a client access token for the provider."""
        pass

    @abstractmethod
    def generate_voice_response(self, room_name: str, should_record: bool, app_base_url: str = ""):
        """Generates the provider-specific response (e.g., TwiML) for a participant to join a conference."""
        pass

    @abstractmethod
    def initiate_outbound_call(self, to_number: str, room_name: str, should_record: bool, callback_url: str):
        """Initiates an outbound call to a participant and directs them into a conference."""
        pass

    @abstractmethod
    def terminate_call(self, call_sid: str):
        """Terminates an active call leg."""
        pass

    @property
    @abstractmethod
    def content_type(self) -> str:
        """The content-type to use for the voice response (e.g., text/xml)."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """The identifier for the calling provider (e.g., 'twilio', 'telnyx')."""
        pass

    @property
    @abstractmethod
    def caller_id(self) -> str:
        """The caller ID (phone number) associated with this provider."""
        pass
