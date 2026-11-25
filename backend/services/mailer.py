"""Outbound email — the delivery seam (Phase 8.2; design 02 §1.3).

Cinemora has no email infrastructure today. This module is the one
place that knows how mail would be sent:

- When SMTP is configured (CINEMORA_SMTP_HOST + CINEMORA_SMTP_FROM,
  optional PORT/USER/PASSWORD/STARTTLS), mail is sent via smtplib
  and the delivery mode is "smtp".
- Otherwise NOTHING is sent and the mode is "manual": the caller
  must surface that honestly (the password-reset flow reports the
  mode to the requester). Secret-bearing content — a password-reset
  token or the complete reset link — must NEVER be written to a log
  (B-08/E-24): logs are not a delivery channel. Callers in manual
  mode may log only a redacted record (recipient, purpose, expiry,
  a short hash-prefix reference).

The mode is never faked: "smtp" is returned only after the server
accepts the message.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(
        os.environ.get("CINEMORA_SMTP_HOST")
        and os.environ.get("CINEMORA_SMTP_FROM")
    )


def send_email(to: str, subject: str, body: str) -> str:
    """Returns the delivery mode: "smtp" or "manual"."""
    if not smtp_configured():
        logger.info(
            "mailer: SMTP not configured; message for %s NOT sent "
            "(manual delivery mode). Subject: %s",
            to,
            subject,
        )
        return "manual"

    message = EmailMessage()
    message["From"] = os.environ["CINEMORA_SMTP_FROM"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    host = os.environ["CINEMORA_SMTP_HOST"]
    port = int(os.environ.get("CINEMORA_SMTP_PORT", "587"))
    user = os.environ.get("CINEMORA_SMTP_USER")
    password = os.environ.get("CINEMORA_SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if os.environ.get("CINEMORA_SMTP_STARTTLS", "1") != "0":
            smtp.starttls()
        if user:
            smtp.login(user, password or "")
        smtp.send_message(message)
    return "smtp"
