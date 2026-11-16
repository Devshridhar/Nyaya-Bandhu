from __future__ import annotations

import base64
import hashlib
import hmac
from types import SimpleNamespace

import pytest

from app.core import security
from app.services import whatsapp_router


def test_aes_gcm_round_trip_and_aad_authentication():
    key = security.new_case_key()
    encrypted = security.encrypt_text(key, "private complaint", "case-a:transcript")
    assert security.decrypt_text(key, encrypted, "case-a:transcript") == "private complaint"
    with pytest.raises(Exception):
        security.decrypt_text(key, encrypted, "case-a:other-field")


def test_hmac_blind_index_is_stable_and_not_plaintext():
    first = security.blind_index("utr", "1234 5678 9012")
    assert first == security.blind_index("utr", "123456789012")
    assert first != "123456789012"
    assert len(first) == 64


def test_pdf_tokens_are_bound_to_case_and_expiry():
    token = security.create_pdf_token("NB-2026-ABC123", expires_in_seconds=60)
    assert security.verify_pdf_token("NB-2026-ABC123", token)
    assert not security.verify_pdf_token("NB-2026-OTHER", token)
    assert not security.verify_pdf_token("NB-2026-ABC123", "bad-token")


def test_twilio_signature_uses_sorted_form_parameters(monkeypatch):
    token = "unit-test-auth-token"
    monkeypatch.setattr(whatsapp_router, "get_settings", lambda: SimpleNamespace(twilio_auth_token=token))
    url = "https://example.test/api/v1/webhooks/whatsapp"
    params = {"Body": "hello", "From": "whatsapp:+919000000000"}
    raw = url + "Bodyhello" + "Fromwhatsapp:+919000000000"
    signature = base64.b64encode(hmac.new(token.encode(), raw.encode(), hashlib.sha1).digest()).decode()
    assert whatsapp_router.verify_twilio_signature(url, params, signature)
    assert not whatsapp_router.verify_twilio_signature(url, params, "wrong")
    assert not whatsapp_router.verify_twilio_signature(url.replace("https", "http"), params, signature)
