"""Rate-limit keys avoid putting phone numbers or other PII in limiter storage."""
from __future__ import annotations

from limits.util import parse
from slowapi import Limiter
from fastapi import HTTPException


def rate_limit_key(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    address = forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown")
    return f"ip:{address}"


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=["60/minute"],
    application_limits=["60/minute"],
)


def enforce_user_hourly_limit(contact_hash: str) -> None:
    if not limiter.limiter.hit(parse("10/hour"), f"user:{contact_hash}"):
        raise HTTPException(status_code=429, detail="This phone number has reached the 10-request hourly limit.")
