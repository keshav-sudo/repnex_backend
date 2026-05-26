from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def send_email(*, to: str, subject: str, body: str) -> None:
    s = get_settings()
    if s.EMAIL_PROVIDER == "console" or not s.SMTP_HOST:
        log.info("email_sent_console", extra={"to": to, "subject": subject, "body": body})
        return

    msg = EmailMessage()
    msg["From"] = s.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT) as smtp:
        smtp.starttls()
        if s.SMTP_USER:
            smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
        smtp.send_message(msg)


def send_invite_email(*, to: str, accept_url: str, org_name: str) -> None:
    body = (
        f"You have been invited to join {org_name} on Repnex.\n\n"
        f"Accept your invite here: {accept_url}\n\n"
        f"This link expires in 24 hours."
    )
    send_email(to=to, subject=f"Invitation to join {org_name}", body=body)
