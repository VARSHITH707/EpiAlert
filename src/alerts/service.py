"""Alert generation service.

AlertService reads detection_results, generates alerts with human-readable
messages (Ollama + deterministic fallback), and writes them to the alerts
table. It delegates SMS dispatch to a pluggable provider from src.sms.

SMS providers implement send_sms(phone_number, message) -> dict with keys:
    success: bool
    provider_response: str  (raw provider response or error text)
    delivery_status: str    (sent | failed)
"""

from __future__ import annotations
from typing import Optional
import logging

from src.database.db import get_connection
from src.sms import MockSMSProvider, ProductionSMSProvider, SMSProvider

from src.alerts.message import generate_alert_message

logger = logging.getLogger("alerts")


# ---------------------------------------------------------------------------
# DB access helpers (moved from src.database.db to keep db.py under 800 lines)
# ---------------------------------------------------------------------------

def get_alerts(
    disease: str = None,
    village: str = None,
    street: str = None,
    week_number: int = None,
    status: str = None,
    limit: int = 100,
) -> list:
    """Query alerts with optional filters."""
    conn = get_connection()
    cur = conn.cursor()
    conditions = ["1=1"]
    params = []
    if disease:
        conditions.append("disease = ?")
        params.append(disease)
    if village:
        conditions.append("village = ?")
        params.append(village)
    if street:
        conditions.append("street = ?")
        params.append(street)
    if week_number:
        conditions.append("week_number = ?")
        params.append(week_number)
    if status:
        conditions.append("status = ?")
        params.append(status)
    cur.execute(
        f"SELECT * FROM alerts WHERE {' AND '.join(conditions)} ORDER BY week_number DESC, created_at DESC LIMIT ?",
        params + [limit],
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_alert(alert_id: int) -> dict:
    """Get a single alert by ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else {}


def get_sms_messages(alert_id: int = None, phone_number: str = None, limit: int = 100) -> list:
    """Query SMS audit rows."""
    conn = get_connection()
    cur = conn.cursor()
    conditions = ["1=1"]
    params = []
    if alert_id:
        conditions.append("alert_id = ?")
        params.append(alert_id)
    if phone_number:
        conditions.append("phone_number = ?")
        params.append(phone_number)
    cur.execute(
        f"SELECT * FROM sms_messages WHERE {' AND '.join(conditions)} ORDER BY sent_at DESC LIMIT ?",
        params + [limit],
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_distinct_phone_numbers() -> list:
    """Return distinct phone numbers from the people table."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT phone_number FROM people WHERE phone_number IS NOT NULL AND phone_number != ''")
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def upsert_person_phone(person_id: str, phone_number: str) -> None:
    """Store or update a person's phone number.

    Uses UPDATE for existing people (who already have village/street),
    INSERT only for new people.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM people WHERE person_id = ?", (person_id,))
    exists = cur.fetchone()[0] > 0
    if exists:
        cur.execute(
            "UPDATE people SET phone_number = ? WHERE person_id = ?",
            (phone_number, person_id),
        )
    else:
        cur.execute(
            "INSERT INTO people (person_id, phone_number) VALUES (?, ?)",
            (person_id, phone_number),
        )
    conn.commit()
    cur.close()
    conn.close()


def _already_sent(alert_id: int, phone_number: str) -> bool:
    """True if this (alert, phone) pair already has a successful send.

    Used as the idempotency gate before contacting the SMS provider, so a
    repeated dispatch never re-sends to a handset that already received it.
    A previously failed row is not treated as sent, so failures can be retried.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT 1 FROM sms_messages
           WHERE alert_id = ? AND phone_number = ?
             AND delivery_status = 'sent'
           LIMIT 1""",
        (alert_id, phone_number),
    )
    found = cur.fetchone() is not None
    cur.close()
    conn.close()
    return found


def upsert_sms_message(
    alert_id: int,
    phone_number: str,
    message: str,
    provider: str,
    provider_response: str = None,
    delivery_status: str = "pending",
    error_text: str = None,
) -> int:
    """Insert or update an SMS audit row. Returns the sms_id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sms_messages
               (alert_id, phone_number, message, provider, provider_response,
                delivery_status, error_text, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(alert_id, phone_number)
               DO UPDATE SET
                   message = excluded.message,
                   provider = excluded.provider,
                   provider_response = excluded.provider_response,
                   delivery_status = excluded.delivery_status,
                   error_text = excluded.error_text,
                   sent_at = CURRENT_TIMESTAMP
               RETURNING sms_id""",
        (alert_id, phone_number, message, provider, provider_response,
         delivery_status, error_text),
    )
    row = cur.fetchone()
    sms_id = row[0] if row else 0
    conn.commit()
    cur.close()
    conn.close()
    return sms_id


# ---------------------------------------------------------------------------
# SMS provider abstraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Alert service
# ---------------------------------------------------------------------------

class AlertService:
    """Reads detection_results, generates alerts with messages, writes to DB.

    Usage:
        svc = AlertService()
        n = svc.generate_alerts(fusion_mode="confirmation")
        sms_service = SMSDispatchService(svc, provider=MockSMSProvider())
        sent = sms_service.dispatch_alert(alerts[0])
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path

    def generate_alerts(
        self,
        fusion_mode: str = "confirmation",
        min_status: str = "ALERT",
        use_ollama: bool = True,
    ) -> int:
        """Create one alert row per (disease, village, street, week) from
        detection_results where status is ALERT or HIGH_ALERT.

        The message column holds a human-readable alert. The explanation column
        holds the diagnostic string from detection_results.explanation.

        sms_eligible is set to 1 ONLY for fusion_mode='confirmation' rows.

        Idempotent: re-running updates existing rows, never duplicates.

        Returns the number of rows created or updated.
        """
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            """SELECT week_number, disease, village, street, status, severity,
                      observed_count, expected_count,
                      cusum_signal, ewma_signal, trend_sustained,
                      baseline_deviation, explanation
               FROM detection_results
               WHERE fusion_mode = ?
                 AND status IN ('ALERT', 'HIGH_ALERT')
               ORDER BY week_number, disease, village, street""",
            (fusion_mode,),
        )

        created = 0
        for row in cur.fetchall():
            (week_number, disease, village, street, status, severity,
             observed_count, expected_count,
             cusum_signal, ewma_signal, trend_sustained,
             baseline_deviation, dr_explanation) = row

            # Reuse the message this alert already has. Regenerating every
            # message on every run meant adding one new alert re-ran the LLM
            # for all of them -- minutes of work for a single row. The
            # underlying values cannot change for an existing (disease,
            # village, street, week), so the text cannot need to change either.
            cur.execute(
                """SELECT message FROM alerts
                   WHERE disease = ? AND village = ? AND street = ?
                     AND week_number = ? AND fusion_mode = ?""",
                (disease, village, street, week_number, fusion_mode),
            )
            existing = cur.fetchone()
            message = existing[0] if existing and existing[0] else None

            if not message:
                # Human-readable message from verified values only.
                message = generate_alert_message(
                    disease=disease,
                    village=village,
                    street=street,
                    week_number=week_number,
                    observed_count=observed_count,
                    expected_count=expected_count,
                    status=status,
                    explanation=dr_explanation,
                    use_ollama=use_ollama,
                )

            # sms_eligible ONLY for confirmation mode alerts.
            sms_eligible = 1 if fusion_mode == "confirmation" else 0

            cusum_stat = float(cusum_signal)
            ewma_stat = float(ewma_signal)
            trend_desc = "Sustained increase" if trend_sustained else "No sustained trend"

            cur.execute(
                """INSERT INTO alerts
                   (disease, village, street, week_number, status, severity,
                    observed_count, expected_count,
                    cusum_signal, ewma_signal,
                    cusum_stat, ewma_stat,
                    trend_description, baseline_deviation, message,
                    sms_eligible, fusion_mode, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(disease, village, street, week_number, fusion_mode)
                   DO UPDATE SET
                       status = excluded.status,
                       severity = excluded.severity,
                       observed_count = excluded.observed_count,
                       expected_count = excluded.expected_count,
                       cusum_signal = excluded.cusum_signal,
                       ewma_signal = excluded.ewma_signal,
                       cusum_stat = excluded.cusum_stat,
                       ewma_stat = excluded.ewma_stat,
                       trend_description = excluded.trend_description,
                       baseline_deviation = excluded.baseline_deviation,
                       message = excluded.message,
                       sms_eligible = excluded.sms_eligible,
                       fusion_mode = excluded.fusion_mode,
                       explanation = excluded.explanation,
                       created_at = CURRENT_TIMESTAMP""",
                (
                    disease, village, street, week_number,
                    status, severity,
                    observed_count, expected_count,
                    cusum_signal, ewma_signal,
                    cusum_stat, ewma_stat,
                    trend_desc, baseline_deviation,
                    message,
                    sms_eligible,
                    fusion_mode,
                    dr_explanation,
                ),
            )
            created += 1

        # Verify fusion_mode was written — the ON CONFLICT below must include it
        # or union/confirmation alerts overwrite each other.
        conn.commit()
        cur.execute(
            "SELECT COUNT(*) FROM alerts WHERE fusion_mode = ?",
            (fusion_mode,),
        )
        count_after = cur.fetchone()[0]
        if count_after != created:
            logger.warning(
                "Alert count mismatch for %s: wrote %d rows, DB has %d. "
                "ON CONFLICT may be missing fusion_mode.",
                fusion_mode, created, count_after,
            )

        conn.commit()
        cur.close()
        conn.close()
        return created

    def get_alert(self, alert_id: int) -> dict:
        """Return one alert by ID."""
        return get_alert(alert_id)

    def list_alerts(
        self,
        disease: str = None,
        village: str = None,
        street: str = None,
        week_number: int = None,
        status: str = None,
        limit: int = 100,
    ) -> list:
        """Return alerts with optional filters."""
        return get_alerts(
            disease=disease,
            village=village,
            street=street,
            week_number=week_number,
            status=status,
            limit=limit,
        )


# ---------------------------------------------------------------------------
# SMS dispatch
# ---------------------------------------------------------------------------

class SMSDispatchService:
    """Dispatches SMS for alerts that are sms_eligible.

    Finds affected people in (village, street) for the alert's week,
    looks up their phone numbers, normalises to E.164, deduplicates,
    and sends through the configured provider. Writes full audit rows.

    Deduplication is the point: there are only 3 distinct phone numbers
    in the dataset, so an alert affecting 400 people sends AT MOST 3
    SMS messages, never 400.
    """

    def __init__(self, alert_service: AlertService, provider: SMSProvider) -> None:
        self.alert_service = alert_service
        self.provider = provider

    def dispatch_alert(self, alert: dict) -> list:
        """Send SMS for one alert to all unique phone numbers in that
        (village, street).

        Returns list of dicts: {phone_number, sms_id, success, delivery_status}.
        Always writes an audit row per (alert_id, phone_number).
        """
        if not alert.get("sms_eligible"):
            logger.info("Alert %s not sms_eligible, skipping SMS", alert["alert_id"])
            return []

        village = alert["village"]
        street = alert["street"]
        alert_id = alert["alert_id"]
        message = alert["message"]

        phone_numbers = self._get_phone_numbers_for_location(village, street)
        if not phone_numbers:
            logger.info(
                "No phone numbers for %s/%s (alert %s)",
                village, street, alert_id,
            )
            return []

        results = []
        for pn in phone_numbers:
            # Idempotency check BEFORE contacting the provider. The UNIQUE
            # constraint protects the audit row, not the handset: without this
            # guard a re-run in live mode would send a duplicate real SMS to
            # every recipient.
            if _already_sent(alert_id, pn):
                logger.info(
                    "Alert %s already sent to %s, skipping", alert_id, pn
                )
                results.append({
                    "phone_number": pn,
                    "sms_id": None,
                    "success": True,
                    "delivery_status": "skipped",
                })
                continue

            resp = self.provider.send_sms(pn, message)
            sms_id = upsert_sms_message(
                alert_id=alert_id,
                phone_number=pn,
                message=message,
                provider=self.provider.name,
                provider_response=resp["provider_response"],
                delivery_status=resp["delivery_status"],
                error_text=None if resp["success"] else resp["provider_response"],
            )
            results.append({
                "phone_number": pn,
                "sms_id": sms_id,
                "success": resp["success"],
                "delivery_status": resp["delivery_status"],
            })
        return results

    def _get_phone_numbers_for_location(self, village: str, street: str) -> list:
        """Return distinct E.164 phone numbers for people in (village, street)."""
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT p.phone_number
               FROM people p
               WHERE p.village = ? AND p.street = ?
                 AND p.phone_number IS NOT NULL
                 AND p.phone_number != ''""",
            (village, street),
        )
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        return [_normalise_phone(pn) for pn in rows if _normalise_phone(pn)]

    def dispatch_all_sms_eligible(self) -> dict:
        """Dispatch SMS for ALL sms_eligible alerts. Returns summary counts.

        Covers both ALERT and HIGH_ALERT. Filtering to "ALERT" alone would
        silently skip every HIGH_ALERT, i.e. the most severe cases.

        Counts sent and skipped separately: a skipped message was already
        delivered for that (alert, phone) pair and must not be reported as a
        new send.
        """
        # list_alerts defaults to limit=100, which would silently dispatch only
        # the first 100 alerts of each status. Pass an explicit high limit so
        # every eligible alert is covered.
        alerts = [
            a
            for status in ("ALERT", "HIGH_ALERT")
            for a in self.alert_service.list_alerts(status=status, limit=100000)
        ]
        sms_eligible = [a for a in alerts if a.get("sms_eligible")]
        total_sent = 0
        total_skipped = 0
        total_failed = 0
        by_provider = {}
        for a in sms_eligible:
            results = self.dispatch_alert(a)
            for r in results:
                if r["delivery_status"] == "skipped":
                    total_skipped += 1
                elif r["success"]:
                    total_sent += 1
                else:
                    total_failed += 1
            by_provider[self.provider.name] = by_provider.get(self.provider.name, 0) + len(results)
        return {
            "provider": self.provider.name,
            "alerts_dispatched": len(sms_eligible),
            "sms_sent": total_sent,
            "sms_skipped": total_skipped,
            "sms_failed": total_failed,
            "by_provider": by_provider,
        }


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

def _normalise_phone(raw: str) -> Optional[str]:
    """Normalise a phone number to E.164-like format.

    The dataset phone numbers are local formats. We strip non-digits and
    prefix with +91 (India) if the number has 10 digits. If the number
    already starts with + it is returned as-is. Returns None if the number
    cannot be normalised.
    """
    import re
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 8:
            return f"+{digits}"
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if len(digits) >= 8:
        return f"+91{digits}"
    return None
