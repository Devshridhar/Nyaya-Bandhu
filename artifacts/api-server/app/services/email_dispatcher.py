"""Resend/SMTP delivery with development sandboxing and explicit mock states."""
from __future__ import annotations

import base64
import html
import logging
from email.message import EmailMessage

import aiosmtplib
import httpx

from app.core.config import get_settings
from app.services.ai_client import external_retry

logger = logging.getLogger("nyaya.email")


@external_retry
async def _send_resend(to_email: str, subject: str, body: str, pdf_bytes: bytes, filename: str, idempotency_key: str) -> str:
    settings = get_settings()
    payload = {
        "from": settings.resend_from_email,
        "to": [to_email],
        "subject": subject,
        "text": body,
        "html": "<p>" + html.escape(body).replace("\n", "<br>") + "</p>",
        "attachments": [{"filename": filename, "content": base64.b64encode(pdf_bytes).decode("ascii")}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
    return str(result.get("id", "sent"))


@external_retry
async def _send_smtp(to_email: str, subject: str, body: str, pdf_bytes: bytes, filename: str) -> str:
    settings = get_settings()
    message = EmailMessage()
    message["From"] = settings.smtp_from_email or settings.smtp_user or "Nyaya Bandhu"
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    result = await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_pass,
        start_tls=settings.smtp_port == 587,
        timeout=30,
    )
    return str(result[0] if isinstance(result, tuple) else result)


async def dispatch_nodal_email(payload: dict, pdf_bytes: bytes, case_reference: str) -> dict:
    if not payload.get("to_email") or not payload.get("email_verified"):
        return {"status": "MOCK_NOT_SENT", "message_id": None, "reason": "No verified merchant grievance address is available."}
    settings = get_settings()
    destination = payload["to_email"]
    status = "SENT"
    if not settings.is_production:
        if not settings.email_sandbox_recipient:
            return {"status": "MOCK_NOT_SENT", "message_id": None, "reason": "Development mode does not send email to merchants."}
        destination = settings.email_sandbox_recipient
        status = "SENT_SANDBOX"
    elif not settings.email_live_dispatch:
        return {"status": "MOCK_NOT_SENT", "message_id": None, "reason": "Live email dispatch is disabled."}

    subject = payload["subject"]
    if status == "SENT_SANDBOX":
        subject = f"[SANDBOX] {subject}"
    if settings.resend_api_key:
        message_id = await _send_resend(
            destination,
            subject,
            payload["body_text"],
            pdf_bytes,
            f"{case_reference}_complaint.pdf",
            f"nyaya-{case_reference}",
        )
    elif settings.smtp_host:
        message_id = await _send_smtp(destination, subject, payload["body_text"], pdf_bytes, f"{case_reference}_complaint.pdf")
    else:
        return {"status": "MOCK_NOT_SENT", "message_id": None, "reason": "No email provider is configured."}
    logger.info("nodal_email_dispatched", extra={"case_reference": case_reference, "event": status.lower()})
    return {"status": status, "message_id": message_id, "reason": None}
