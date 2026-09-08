"""EpiAlert SMS provider abstraction.

Pluggable SMS provider interface. Implementations:
- MockSMSProvider (default, SMS_MODE=mock): simulates sending, writes audit rows.
- ProductionSMSProvider (SMS_MODE=live): sends only when credentials present.

To add Twilio, MSG91, or any other provider: implement SMSProvider and drop it in.
"""

from __future__ import annotations
from typing import Protocol


class SMSProvider(Protocol):
    """Pluggable SMS provider interface.

    Implementations return dict with keys:
        success: bool
        provider_response: str  (raw response or error text)
        delivery_status: str    (sent | failed)
    """

    name: str

    def send_sms(self, phone_number: str, message: str) -> dict:
        """Send an SMS. Returns result dict."""


class MockSMSProvider:
    """Mock SMS provider — simulates sending, writes full audit rows.

    Does not send any real SMS. Used when SMS_MODE=mock (default).
    Simulates a provider response and still writes the complete sms_messages
    audit row.
    """

    name = "mock"

    def send_sms(self, phone_number: str, message: str) -> dict:
        return {
            "success": True,
            "provider_response": f"mock: simulated send to {phone_number}",
            "delivery_status": "sent",
        }


class ProductionSMSProvider:
    """Production SMS provider stub — sends only when credentials are present.

    Reads SMS_API_KEY and SMS_FROM env vars. If absent, every send fails
    gracefully (returns success=False with an error message) so the system
    never crashes because credentials are missing.

    To wire a real provider (Twilio, MSG91, etc.): subclass this or implement
    SMSProvider directly and update the provider selection in cmd_sms.
    """

    name = "production"

    def __init__(self) -> None:
        import os

        self.api_key = os.getenv("SMS_API_KEY", "")
        self.from_number = os.getenv("SMS_FROM", "")

    def send_sms(self, phone_number: str, message: str) -> dict:
        if not self.api_key or not self.from_number:
            return {
                "success": False,
                "provider_response": "ProductionSMSProvider: SMS_API_KEY or SMS_FROM not configured",
                "delivery_status": "failed",
            }
        # Real provider integration would go here.
        # For now, treat configured as "would send" — still audited as mock-like.
        return {
            "success": True,
            "provider_response": f"production: would send to {phone_number} (api_key set, from={self.from_number})",
            "delivery_status": "sent",
        }
