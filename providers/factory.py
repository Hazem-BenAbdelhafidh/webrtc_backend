import os
from providers.twilio_provider import TwilioProvider
from providers.telnyx_provider import TelnyxProvider

def get_provider():
    """
    Returns an instance of the provider based on the environment variable CALLING_PROVIDER.
    Defaults to 'twilio'.
    """
    provider_name = os.getenv("CALLING_PROVIDER", "twilio").lower()

    if provider_name == "twilio":
        return TwilioProvider()
    elif provider_name == "telnyx":
        return TelnyxProvider()
    else:
        raise ValueError(f"Unsupported provider: {provider_name}")
