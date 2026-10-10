"""Multi-channel notifications for Disney dining alerts.

Recipient routing (prefix-based, applied per-recipient):

- ``signal:+15551234567`` -> Signal via local signal-cli (preferred for personal use)
- ``whatsapp:+15551234567`` -> Twilio Programmable Messaging WhatsApp channel
- ``+15551234567`` (or any non-prefixed string starting with ``+``) -> Twilio SMS

The same dispatcher serves both reservation alerts (send_sms) and operational
alerts (send_operational_sms) so a Signal-only owner gets every alert via
Signal regardless of which code path triggered it.
"""

import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from twilio.rest import Client

from monitor import Slot

load_dotenv()

# Disney's SPA does not honor date/time/partySize/offerId on a cold URL load —
# it reads that state from the authenticated session after the booking flow has
# already populated it. So we link to the restaurant's own dining page (the same
# URL the bot itself navigates to before polling) and put the slot details in
# the message body. See plan: deep-link research, May 2026.
BOOK_BASE = "https://disneyworld.disney.go.com/dine-res/restaurant"
MAX_MESSAGE_LENGTH = 1500
SIGNAL_CLI_PATH = os.environ.get("SIGNAL_CLI_PATH", "/usr/local/bin/signal-cli")
SIGNAL_SEND_TIMEOUT = 60  # seconds; signal-cli send is usually <5s but pad for slow networks


@dataclass
class SendResult:
    sent_slots: List[Slot]
    errors: List[str]


def booking_url(slot: Slot) -> str:
    # Use the human-readable slug when available; fall back to the numeric
    # facility id for any older cached slot that somehow lacks one.
    slug = (slot.slug or slot.facility_id).strip("/")
    if slot.booking_type == "scheduled_activity":
        # Enchanting Extras experiences book through their own flow; there is
        # no per-offer deep link.
        return f"https://disneyworld.disney.go.com/enchanting-extras-collection/{slug}/"
    return f"{BOOK_BASE}/{slug}/"


def _format_message(slots: List[Slot]) -> str:
    title = "Disney Dining Alert!" if len(slots) == 1 else f"Disney Dining Alert! {len(slots)} new openings"
    lines = [title]

    for slot in sorted(slots, key=lambda s: (s.date, s.restaurant_name, s.time, s.party_size)):
        time_label = slot.label or slot.time
        lines.append(f"\nNew opening: {slot.restaurant_name}")
        lines.append(f"{slot.date} at {time_label} | Party of {slot.party_size} | {slot.meal_period}")
        lines.append(f"Book: {booking_url(slot)}")

    lines.append("\nReply STOP to opt out. Reply HELP for help.")
    return "\n".join(lines)


A2P_FOOTER = "\n\nReply STOP to opt out. Reply HELP for help."


# ── channel routing ────────────────────────────────────────────────────────

def _classify(recipient: str) -> Tuple[str, str]:
    """Return (channel, normalized_recipient) for a configured recipient string.

    channel is one of: "signal", "whatsapp", "sms".
    normalized_recipient is always a plain E.164 number with the "+" prefix,
    stripped of any channel prefix.
    """
    r = (recipient or "").strip()
    if r.lower().startswith("signal:"):
        return "signal", r[len("signal:"):].strip()
    if r.lower().startswith("whatsapp:"):
        return "whatsapp", r[len("whatsapp:"):].strip()
    return "sms", r


def _send_via_signal(to_number: str, body: str) -> Optional[str]:
    """Send a Signal message via local signal-cli. Returns None on success, error string on failure."""
    bot_number = os.environ.get("SIGNAL_BOT_NUMBER", "").strip()
    if not bot_number:
        return "SIGNAL_BOT_NUMBER not set in .env"
    if not os.path.exists(SIGNAL_CLI_PATH):
        return f"signal-cli not found at {SIGNAL_CLI_PATH}"
    try:
        result = subprocess.run(
            [SIGNAL_CLI_PATH, "-a", bot_number, "send", "-m", body, to_number],
            capture_output=True,
            text=True,
            timeout=SIGNAL_SEND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"signal-cli timed out after {SIGNAL_SEND_TIMEOUT}s sending to {to_number}"
    except Exception as exc:
        return f"signal-cli crashed sending to {to_number}: {type(exc).__name__}: {exc}"

    if result.returncode != 0:
        err_tail = (result.stderr or result.stdout or "").strip()[-300:]
        return f"signal-cli exit {result.returncode} sending to {to_number}: {err_tail}"

    timestamp = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    print(f"[notify] Signal message delivered to {to_number}. {timestamp}")
    return None


_twilio_client: Optional[Client] = None


def _twilio() -> Optional[Client]:
    global _twilio_client
    if _twilio_client is not None:
        return _twilio_client
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not sid or not tok:
        return None
    _twilio_client = Client(sid, tok)
    return _twilio_client


def _send_via_twilio(to_recipient_with_prefix: str, body: str, channel: str) -> Optional[str]:
    """Send via Twilio. to_recipient_with_prefix retains any channel prefix Twilio expects.

    For SMS, prefer TWILIO_MESSAGING_SERVICE_SID (required for US A2P 10DLC
    delivery via the registered campaign — Twilio picks the registered sender
    from the pool and we must NOT pass from_). Fall back to TWILIO_FROM.

    For WhatsApp, Messaging Service routing does not apply to the sandbox
    sender, so always use from_=TWILIO_FROM (which must be "whatsapp:+...").
    """
    client = _twilio()
    if client is None:
        return "Twilio credentials not set"

    create_kwargs = {"body": body, "to": to_recipient_with_prefix}
    if channel == "sms":
        messaging_service_sid = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip()
        if messaging_service_sid:
            create_kwargs["messaging_service_sid"] = messaging_service_sid
        else:
            from_number = os.environ.get("TWILIO_FROM", "").strip()
            if not from_number:
                return "Neither TWILIO_MESSAGING_SERVICE_SID nor TWILIO_FROM is set"
            create_kwargs["from_"] = from_number
    else:
        from_number = os.environ.get("TWILIO_FROM", "").strip()
        if not from_number:
            return "TWILIO_FROM not set"
        create_kwargs["from_"] = from_number

    try:
        msg = client.messages.create(**create_kwargs)
        print(f"[notify] Twilio message accepted for {to_recipient_with_prefix}. SID: {msg.sid}")
        return None
    except Exception as exc:
        return f"Twilio send failed for {to_recipient_with_prefix}: {exc}"


def _dispatch(recipient: str, body: str) -> Optional[str]:
    """Send body to recipient via the appropriate channel. Returns None on success, error string on failure."""
    channel, normalized = _classify(recipient)
    if not normalized:
        return f"empty recipient: {recipient!r}"

    if channel == "signal":
        return _send_via_signal(normalized, body)

    # Twilio expects "whatsapp:+..." or just "+..." — pass the original prefixed string
    twilio_recipient = recipient if channel == "whatsapp" else normalized
    return _send_via_twilio(twilio_recipient, body, channel)


# ── public entry points ────────────────────────────────────────────────────

def send_operational_sms(to_numbers: List[str], body: str) -> List[str]:
    """Send a one-off operational message to explicit recipients (session alerts, etc.).

    Appends the A2P compliance footer when the body does not already include
    STOP/HELP. Routes per-recipient via the channel implied by their prefix.
    Returns a list of human-readable errors for sends that failed.
    """
    deduped = list(dict.fromkeys(p.strip() for p in to_numbers if p and p.strip()))
    if not deduped:
        return []

    full_body = body.rstrip()
    if "Reply STOP" not in full_body:
        full_body = full_body + A2P_FOOTER
    if len(full_body) > MAX_MESSAGE_LENGTH:
        full_body = full_body[: MAX_MESSAGE_LENGTH - 3] + "..."

    errors: List[str] = []
    for recipient in deduped:
        err = _dispatch(recipient, full_body)
        if err:
            errors.append(err)
            print(f"[notify] {err}")
    return errors


def send_sms(slots: List[Slot]) -> SendResult:
    if not slots:
        return SendResult([], [])

    try:
        import supabase_pipe
        if supabase_pipe.is_supabase_enabled():
            supabase_pipe.pipe_slots_to_supabase(slots, booking_url)
    except Exception as exc:
        print(f'[notify] Supabase pipe failed: {exc}')

    by_recipient = defaultdict(list)
    fallback_to = os.environ.get("TWILIO_TO", "")
    for slot in slots:
        recipient = slot.recipient_phone or fallback_to
        if recipient:
            by_recipient[recipient].append(slot)

    if not by_recipient:
        error = "No recipient phone configured"
        print(f"[notify] {error} — printing alert instead:")
        print(_format_message(slots))
        return SendResult([], [error])

    sent_slots: List[Slot] = []
    errors: List[str] = []
    for recipient, recipient_slots in by_recipient.items():
        body = _format_message(recipient_slots)
        if len(body) > MAX_MESSAGE_LENGTH:
            body = body[: MAX_MESSAGE_LENGTH - 3] + "..."
            print(f"[notify] Alert for {recipient} truncated to {MAX_MESSAGE_LENGTH} characters.")
        err = _dispatch(recipient, body)
        if err:
            errors.append(err)
            print(f"[notify] {err}")
        else:
            sent_slots.extend(recipient_slots)
    return SendResult(sent_slots, errors)
