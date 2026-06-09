from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _send_smtp(*, to: str, subject: str, body_text: str, body_html: str | None = None) -> None:
    """Synchronous SMTP send — run inside executor to avoid blocking event loop."""
    s = get_settings()
    if s.EMAIL_PROVIDER == "console" or not s.SMTP_HOST:
        log.info(
            "email_sent_console", extra={"to": to, "subject": subject, "body": body_text[:200]}
        )
        return

    if body_html:
        msg = MIMEMultipart("alternative")
        msg["From"] = s.SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = EmailMessage()
        msg["From"] = s.SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body_text)

    try:
        if s.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(s.SMTP_HOST, s.SMTP_PORT) as smtp:
                if s.SMTP_USER:
                    smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT) as smtp:
                try:
                    smtp.starttls()
                except Exception:
                    pass
                if s.SMTP_USER:
                    smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
                smtp.send_message(msg)
        log.info("email_sent_smtp", extra={"to": to, "subject": subject})
    except Exception as e:
        log.error("email_smtp_connection_failed", extra={"to": to, "err": str(e)})
        raise


def send_email(*, to: str, subject: str, body: str) -> None:
    """Legacy sync wrapper — prefer send_email_async in async context."""
    _send_smtp(to=to, subject=subject, body_text=body)


async def send_email_async(
    *, to: str, subject: str, body_text: str, body_html: str | None = None
) -> None:
    """Non-blocking email send via thread executor."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _send_smtp(to=to, subject=subject, body_text=body_text, body_html=body_html),
        )
    except Exception as e:
        log.error("email_send_failed", extra={"to": to, "error": str(e)})
        raise


def send_invite_email(*, to: str, accept_url: str, org_name: str) -> None:
    body_text = (
        f"You have been invited to join {org_name} on Repnex.\n\n"
        f"Accept your invite here: {accept_url}\n\n"
        f"This link expires in 24 hours."
    )
    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0;font-family:'Segoe UI',Roboto,sans-serif;background:#f4f4f7;">
      <div style="max-width:520px;margin:40px auto;background:#ffffff;border-radius:16px;
                  box-shadow:0 4px 24px rgba(0,0,0,0.08);overflow:hidden;">
        <!-- Header -->
        <div style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:32px 24px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-0.3px;">
            Repnex
          </h1>
          <p style="margin:6px 0 0;color:rgba(255,255,255,0.85);font-size:13px;">
            AI-Powered ERP Intelligence
          </p>
        </div>
        <!-- Body -->
        <div style="padding:32px 24px;">
          <h2 style="margin:0 0 8px;color:#111827;font-size:18px;font-weight:600;">
            You&rsquo;re invited! 🎉
          </h2>
          <p style="margin:0 0 20px;color:#6b7280;font-size:14px;line-height:1.6;">
            <strong>{org_name}</strong> has invited you to collaborate on Repnex &mdash; 
            your AI-powered ERP reporting platform. Connect your databases, ask questions 
            in plain English, and get instant insights.
          </p>
          <!-- CTA Button -->
          <div style="text-align:center;margin:28px 0;">
            <a href="{accept_url}" 
               style="display:inline-block;background:linear-gradient(135deg,#2563eb,#1d4ed8);
                      color:#ffffff;text-decoration:none;padding:14px 36px;border-radius:12px;
                      font-size:15px;font-weight:600;letter-spacing:0.3px;
                      box-shadow:0 4px 14px rgba(37,99,235,0.35);">
              Accept Invitation
            </a>
          </div>
          <p style="margin:24px 0 0;color:#9ca3af;font-size:12px;line-height:1.5;text-align:center;">
            This invitation link expires in <strong>24 hours</strong>.<br>
            If you didn&rsquo;t expect this, you can safely ignore this email.
          </p>
        </div>
        <!-- Footer -->
        <div style="background:#f9fafb;padding:16px 24px;text-align:center;
                    border-top:1px solid #e5e7eb;">
          <p style="margin:0;color:#9ca3af;font-size:11px;">
            &copy; Repnex &bull; AI-Powered ERP Reports
          </p>
        </div>
      </div>
    </body>
    </html>
    """
    _send_smtp(
        to=to,
        subject=f"🎉 You're invited to join {org_name} on Repnex",
        body_text=body_text,
        body_html=body_html,
    )


_running_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro) -> None:
    """Safely spawn a background task keeping a strong reference to prevent GC."""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        _running_tasks.add(task)
        task.add_done_callback(_running_tasks.discard)
    except RuntimeError:
        # Fallback if no running loop (e.g. testing)
        asyncio.run(coro)

