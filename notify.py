"""
Critical result alerts to the referring doctor.

When triage marks a report stat and the radiologist signs it, the referrer
should hear about it before the courier arrives. Three channels:

    email     - any SMTP server            (ALERT_SMTP_* secrets)
    sms       - Twilio REST API            (TWILIO_* secrets)
    whatsapp  - Twilio's WhatsApp channel  (same TWILIO_* secrets)

Same contract as the Gemini paths: the message builders are pure and tested
offline; the live send needs credentials in Streamlit secrets and is exercised
by live_check.py. With nothing configured the app simply does not offer the
button - it never pretends to have sent something.

Alerts are sent when the radiologist clicks, never automatically. An
auto-sent alert on a false-positive triage would train every referrer to
ignore the channel, which is worse than no channel.

The message names the patient - the referrer cannot act on "a patient of
yours" - so the recipient number/address must be the referring doctor's own,
entered by the clinic. Nothing here posts to numbers found in report text.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

TIMEOUT = 15


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _secret(name: str) -> str:
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return os.environ.get(name, "").strip()


def smtp_config() -> dict | None:
    host = _secret("ALERT_SMTP_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(_secret("ALERT_SMTP_PORT") or "587"),
        "user": _secret("ALERT_SMTP_USER"),
        "password": _secret("ALERT_SMTP_PASSWORD"),
        "sender": _secret("ALERT_FROM") or _secret("ALERT_SMTP_USER"),
    }


def twilio_config() -> dict | None:
    sid = _secret("TWILIO_ACCOUNT_SID")
    token = _secret("TWILIO_AUTH_TOKEN")
    sender = _secret("TWILIO_FROM")
    if not (sid and token and sender):
        return None
    return {"sid": sid, "token": token, "sender": sender}


def channels_available() -> list[str]:
    out = []
    if smtp_config():
        out.append("email")
    if twilio_config():
        out.extend(["sms", "whatsapp"])
    return out


# --------------------------------------------------------------------------- #
# Message building - pure, offline-testable
# --------------------------------------------------------------------------- #


@dataclass
class Alert:
    subject: str
    body: str    # full text for email / WhatsApp
    short: str   # one SMS worth


def build_alert(record: dict) -> Alert:
    """The alert for one signed report, from its record (records.py shape)."""
    urgency = str(record.get("urgency") or "routine").upper()
    patient = record.get("patient") or "an unnamed patient"
    study = record.get("study") or "a radiology study"
    terms = ", ".join(record.get("triage_terms") or []) or "critical finding"
    referrer = record.get("referrer") or "Doctor"
    signed_by = record.get("signed_by") or "the reporting radiologist"

    subject = f"[{urgency}] {terms} - {patient}"
    body = "\n".join([
        f"Dear {referrer},",
        "",
        f"A {urgency} finding needs your attention.",
        "",
        f"Patient:  {patient} ({record.get('age_sex', '') or 'age/sex not stated'})",
        f"Study:    {study}",
        f"Finding:  {terms}",
        f"Signed:   {record.get('signed_at') or record.get('updated') or ''} "
        f"by {signed_by}",
        "",
        "The full report follows by the usual channel. Please acknowledge receipt.",
        "",
        "Sent by HC FORMAT report system.",
    ])
    short = f"{urgency}: {terms}. {patient}, {study}. Full report to follow. - HC FORMAT"
    return Alert(subject=subject, body=body, short=short[:320])


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


@dataclass
class SendResult:
    channel: str
    ok: bool
    detail: str


def send_email(to: str, alert: Alert) -> SendResult:
    config = smtp_config()
    if not config:
        return SendResult("email", False, "SMTP is not configured (ALERT_SMTP_HOST).")
    message = EmailMessage()
    message["From"] = config["sender"]
    message["To"] = to
    message["Subject"] = alert.subject
    message.set_content(alert.body)
    try:
        with smtplib.SMTP(config["host"], config["port"], timeout=TIMEOUT) as server:
            server.starttls(context=ssl.create_default_context())
            if config["user"]:
                server.login(config["user"], config["password"])
            server.send_message(message)
        return SendResult("email", True, f"Sent to {to}.")
    except Exception as exc:
        return SendResult("email", False, f"{type(exc).__name__}: {exc}")


def _twilio_send(to: str, body: str, whatsapp: bool) -> SendResult:
    channel = "whatsapp" if whatsapp else "sms"
    config = twilio_config()
    if not config:
        return SendResult(channel, False, "Twilio is not configured (TWILIO_ACCOUNT_SID).")
    sender = config["sender"]
    if whatsapp:
        sender = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
        to = to if to.startswith("whatsapp:") else f"whatsapp:{to}"

    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{urllib.parse.quote(config['sid'])}/Messages.json")
    payload = urllib.parse.urlencode({"From": sender, "To": to, "Body": body}).encode()
    request = urllib.request.Request(url, data=payload, method="POST")
    credentials = f"{config['sid']}:{config['token']}".encode()
    import base64 as _b64

    request.add_header("Authorization", f"Basic {_b64.b64encode(credentials).decode()}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
            sid = data.get("sid", "")
            return SendResult(channel, True, f"Queued by Twilio ({sid}).")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("message", str(exc))
        except Exception:
            detail = str(exc)
        return SendResult(channel, False, f"Twilio refused: {detail}")
    except Exception as exc:
        return SendResult(channel, False, f"{type(exc).__name__}: {exc}")


def send_sms(to: str, alert: Alert) -> SendResult:
    return _twilio_send(to, alert.short, whatsapp=False)


def send_whatsapp(to: str, alert: Alert) -> SendResult:
    return _twilio_send(to, alert.body, whatsapp=True)


_SENDERS = {"email": send_email, "sms": send_sms, "whatsapp": send_whatsapp}


def send_alert(record: dict, to: str, channels: list[str]) -> list[SendResult]:
    """
    Fire one alert over the chosen channels. Never raises - each channel
    reports its own success or failure and the app shows both honestly.
    """
    alert = build_alert(record)
    results = []
    for channel in channels:
        sender = _SENDERS.get(channel)
        if sender is None:
            results.append(SendResult(channel, False, f"Unknown channel “{channel}”."))
            continue
        results.append(sender(to, alert))
    return results
