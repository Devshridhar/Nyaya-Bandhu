"""Twilio webhook signature checks, safe media download, and reply delivery."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.services.ai_client import external_retry

logger = logging.getLogger("nyaya.whatsapp")
ALLOWED_MEDIA_HOSTS = {"api.twilio.com", "media.twiliocdn.com"}


def verify_twilio_signature(url: str, params, signature: str | None) -> bool:
    token = get_settings().twilio_auth_token
    if not token or not signature:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    body = url
    for key in sorted(params.keys()):
        values = params.getlist(key) if hasattr(params, "getlist") else [params[key]]
        for value in sorted(str(item) for item in values):
            body += f"{key}{value}"
    digest = hmac.new(token.encode(), body.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


@external_retry
async def download_twilio_media(url: str) -> tuple[bytes, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_MEDIA_HOSTS or parsed.username or parsed.password:
        raise ValueError("WhatsApp media URL is not an allowed Twilio media host.")
    settings = get_settings()
    current = url
    for _ in range(4):
        host = urlparse(current).hostname
        if host not in ALLOWED_MEDIA_HOSTS:
            raise ValueError("Twilio media redirected to an untrusted host.")
        auth = (settings.twilio_account_sid, settings.twilio_auth_token) if host == "api.twilio.com" else None
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            async with client.stream("GET", current, auth=auth) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Twilio media redirect omitted its destination.")
                    if location.startswith("/"):
                        from urllib.parse import urljoin
                        location = urljoin(current, location)
                    current = location
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise ValueError("WhatsApp media exceeds the 10 MB upload limit.")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    raise ValueError("Too many redirects while fetching WhatsApp media.")


async def send_twilio_reply(to_number: str, body: str) -> None:
    settings = get_settings()
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_whatsapp_number):
        logger.warning("whatsapp_reply_not_sent_missing_configuration", extra={"event": "whatsapp_mock"})
        return
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            endpoint,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            data={
                "From": settings.twilio_whatsapp_number,
                "To": to_number,
                "Body": body[:1500],
            },
        )
        response.raise_for_status()
