"""EpiAlert alert generation and SMS dispatch.

Reads verified detection_results from the database, generates human-readable
alert messages (via Ollama phi3 with a deterministic template fallback), and
dispatches SMS through a pluggable provider abstraction.

Every value in an alert message comes from detection_results — nothing is
invented. The message template is a parameterized format string; Ollama recieves
the exact disease, village, street, week, counts and status and is instructed
to phrase them. If Ollama is unavailable the deterministic template is used.
"""

from src.alerts.message import generate_alert_message, DEFAULT_TEMPLATE
from src.alerts.service import (
    AlertService, SMSDispatchService,
    get_alerts, get_alert, get_sms_messages, get_distinct_phone_numbers,
    upsert_person_phone, upsert_sms_message,
)

# SMS providers live in src.sms — re-export for backward compatibility
from src.sms import MockSMSProvider, ProductionSMSProvider, SMSProvider
