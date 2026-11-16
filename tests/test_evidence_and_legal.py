from __future__ import annotations

import io
import asyncio

from PIL import Image

from app.services.evidence_service import sanitize_image, validate_audio
from app.schemas.payloads import EvidenceExtraction
from app.services import legal_agent
from app.services.legal_agent import (
    build_follow_up,
    evaluate_completeness,
    merge_facts,
)
from app.services.pdf_bundler import MAX_PDF_BYTES, build_pdf_bundle


def make_png() -> bytes:
    image = Image.new("RGB", (90, 50), (255, 255, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_image_is_decoded_metadata_stripped_and_converted():
    clean = sanitize_image(make_png(), "payment.png")
    assert clean.content_type == "image/jpeg"
    assert clean.width == 90 and clean.height == 50
    with Image.open(io.BytesIO(clean.data)) as image:
        assert image.format == "JPEG"
        assert not image.getexif()


def test_image_and_audio_magic_bytes_are_required():
    try:
        sanitize_image(b"not an image", "fake.png")
        assert False, "invalid image should be rejected"
    except ValueError as exc:
        assert "valid JPEG" in str(exc)
    assert validate_audio(b"OggS" + b"\x00" * 32, "voice.ogg") == "audio/ogg"
    try:
        validate_audio(b"not audio", "voice.mp3")
        assert False, "invalid audio should be rejected"
    except ValueError as exc:
        assert "contents do not match" in str(exc)


def test_completeness_and_conflicts_never_fill_missing_values():
    existing = {"merchant_name": "Swiggy", "claim_amount_inr": 500, "_conflicts": {}}
    updated, conflicts = merge_facts(existing, {"merchant_name": "Zomato"}, user_response=False)
    assert updated["merchant_name"] == "Swiggy"
    assert conflicts["merchant_name"] == ["Swiggy", "Zomato"]
    missing = evaluate_completeness(updated, conflicts)
    assert "merchant_name" in missing
    assert "transaction_date" in missing
    assert "utr_or_order_id" in missing
    assert build_follow_up(missing[:2], conflicts, "hi").startswith("कृपया")


def test_explicit_user_response_can_resolve_conflict():
    facts, conflicts = merge_facts(
        {"merchant_name": "Swiggy", "_conflicts": {"merchant_name": ["Swiggy", "Zomato"]}},
        {"merchant_name": "Zomato"},
        user_response=True,
    )
    assert facts["merchant_name"] == "Zomato"
    assert "merchant_name" not in conflicts


def test_text_facts_require_matching_verbatim_quotes(monkeypatch):
    source = (
        "I paid Swiggy ₹500 on 02/11/2026. UTR 123456789012. "
        "My order was not delivered; I want a refund."
    )
    parsed = EvidenceExtraction(
        detected_language="en",
        merchant_name="Swiggy",
        merchant_quote="Swiggy",
        claim_amount_inr=500,
        amount_quote="₹500",
        transaction_date="02/11/2026",
        date_quote="02/11/2026",
        utr_or_order_id="123456789012",
        utr_quote="123456789012",
        grievance_description="Order not delivered; refund requested",
        grievance_quote="My order was not delivered; I want a refund.",
    )

    async def fake_extract(_text, _language):
        return parsed

    monkeypatch.setattr(legal_agent, "_extract_from_text", fake_extract)
    result = asyncio.run(legal_agent.extract_text_facts(source))
    assert result["transaction_date"] == "02/11/2026"
    assert result["utr_or_order_id"] == "123456789012"
    assert result["grievance_description"] == parsed.grievance_quote

    parsed.utr_or_order_id = "999999999999"
    result = asyncio.run(legal_agent.extract_text_facts(source))
    assert "utr_or_order_id" not in result


def test_pdf_bundle_is_under_two_megabytes():
    facts = {
        "merchant_name": "Swiggy",
        "claim_amount_inr": 500,
        "transaction_date": "02/11/2026",
        "utr_or_order_id": "123456789012",
        "grievance_description": "The order was not delivered and a refund is requested.",
    }
    output = build_pdf_bundle("NB-2026-ABC123", facts, "Consumer complaint text.", [make_png()])
    assert output.startswith(b"%PDF")
    assert len(output) < MAX_PDF_BYTES
