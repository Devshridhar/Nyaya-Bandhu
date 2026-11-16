"""AES-GCM field encryption, HMAC indexes, request auth, and signed links."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Header, HTTPException

from app.core.config import get_settings

_settings = get_settings()


def new_case_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt_bytes(key: bytes, value: bytes, aad: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, value, aad.encode("utf-8"))


def decrypt_bytes(key: bytes, value: bytes, aad: str) -> bytes:
    if len(value) < 29:
        raise ValueError("Encrypted value is truncated.")
    return AESGCM(key).decrypt(value[:12], value[12:], aad.encode("utf-8"))


def encrypt_text(key: bytes, value: str, aad: str) -> str:
    return base64.urlsafe_b64encode(encrypt_bytes(key, value.encode("utf-8"), aad)).decode("ascii")


def decrypt_text(key: bytes, value: str, aad: str) -> str:
    return decrypt_bytes(key, base64.urlsafe_b64decode(value.encode("ascii")), aad).decode("utf-8")


def wrap_case_key(case_id: str, key: bytes) -> str:
    return encrypt_text(_settings.master_key, base64.b64encode(key).decode(), f"{case_id}:dek")


def unwrap_case_key(case_id: str, wrapped: str) -> bytes:
    return base64.b64decode(decrypt_text(_settings.master_key, wrapped, f"{case_id}:dek"))


def blind_index(kind: str, value: str) -> str:
    normalized = re.sub(r"\s+", "", value).casefold()
    return hmac.new(_settings.index_key, f"{kind}:{normalized}".encode(), hashlib.sha256).hexdigest()


def contact_index(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError("Enter a valid phone number with country code.")
    return blind_index("contact", digits)


def constant_time_equal(left: str | None, right: str | None) -> bool:
    return bool(left and right and hmac.compare_digest(left.encode(), right.encode()))


async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    configured = _settings.admin_api_key
    if configured:
        if not constant_time_equal(x_api_key, configured):
            raise HTTPException(status_code=401, detail="A valid X-API-Key is required.")
        return
    if _settings.is_production:
        raise HTTPException(status_code=503, detail="Administrative API access is not configured.")


def create_pdf_token(case_reference: str, expires_in_seconds: int = 7 * 24 * 3600) -> str:
    expires = int(time.time()) + expires_in_seconds
    payload = f"{case_reference}:{expires}"
    signature = hmac.new(_settings.index_key, f"pdf:{payload}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(f"{expires}:".encode() + signature).decode().rstrip("=")


def verify_pdf_token(case_reference: str, token: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        expires_raw, signature = decoded.split(b":", 1)
        expires = int(expires_raw.decode())
        if expires < int(time.time()):
            return False
        payload = f"{case_reference}:{expires}"
        expected = hmac.new(_settings.index_key, f"pdf:{payload}".encode(), hashlib.sha256).digest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
