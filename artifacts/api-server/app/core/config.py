"""Validated settings and development/production key handling."""
from __future__ import annotations

import base64
import os
import secrets
import threading
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: str = "development"
    database_url: str | None = None
    sqlite_path: str = "artifacts/api-server/.local-data/nyaya-bandhu.sqlite3"
    default_object_storage_bucket_id: str | None = None
    openai_api_key: str | None = None
    ai_integrations_openai_api_key: str | None = None
    ai_integrations_openai_base_url: str | None = None
    master_aes_key: str | None = None
    hmac_blind_index_key: str | None = None
    session_secret: str | None = None
    admin_api_key: str | None = None
    resend_api_key: str | None = None
    resend_from_email: str = "Nyaya Bandhu <onboarding@resend.dev>"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_from_email: str | None = None
    email_live_dispatch: bool = False
    email_sandbox_recipient: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_whatsapp_number: str | None = None
    sarvam_api_key: str | None = None
    bhashini_user_id: str | None = None
    bhashini_ulca_api_key: str | None = None
    bhashini_inference_key: str | None = None
    bhashini_service_id: str | None = None
    enforce_twilio_signature: bool = Field(
        default_factory=lambda: os.getenv("APP_ENV", "development").lower() in {"production", "prod"}
    )
    data_retention_days: int = Field(default=7, ge=1, le=365)
    ai_timeout_seconds: float = Field(default=45, ge=5, le=180)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)

    @field_validator("master_aes_key")
    @classmethod
    def validate_aes_key(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError("MASTER_AES_KEY must be base64 encoding of exactly 32 bytes") from exc
        if len(decoded) != 32:
            raise ValueError("MASTER_AES_KEY must be base64 encoding of exactly 32 bytes")
        return value

    @model_validator(mode="after")
    def validate_production_auth(self) -> "Settings":
        if self.is_production and not (self.master_aes_key or self.session_secret):
            raise ValueError("Production requires MASTER_AES_KEY or SESSION_SECRET for encryption.")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def is_postgres(self) -> bool:
        return bool(self.database_url and "postgres" in self.database_url.lower())

    @property
    def email_mode(self) -> str:
        return "resend" if self.resend_api_key else "smtp" if self.smtp_host else "mock"

    @property
    def crypto_ready(self) -> bool:
        try:
            self.master_key
            self.index_key
            return True
        except ValueError:
            return False

    @property
    def master_key(self) -> bytes:
        if self.master_aes_key:
            return base64.b64decode(self.master_aes_key)
        if self.session_secret:
            return HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"nyaya-bandhu-master-key-v1",
                info=b"case-data-encryption",
            ).derive(self.session_secret.encode())
        if self.is_production:
            raise ValueError("Configure MASTER_AES_KEY or SESSION_SECRET before production startup.")
        return _ephemeral_key("master")

    @property
    def index_key(self) -> bytes:
        if self.hmac_blind_index_key:
            return self.hmac_blind_index_key.encode()
        if self.session_secret:
            return HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"nyaya-bandhu-index-key-v1",
                info=b"blind-index",
            ).derive(self.session_secret.encode())
        if self.is_production:
            raise ValueError("Configure HMAC_BLIND_INDEX_KEY or SESSION_SECRET before production startup.")
        return _ephemeral_key("index")


_ephemeral_keys: dict[str, bytes] = {}
_key_lock = threading.Lock()


def _ephemeral_key(purpose: str) -> bytes:
    # Development-only convenience. Restarting without a configured session
    # secret intentionally makes previous encrypted data unreadable.
    with _key_lock:
        return _ephemeral_keys.setdefault(purpose, secrets.token_bytes(32))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
