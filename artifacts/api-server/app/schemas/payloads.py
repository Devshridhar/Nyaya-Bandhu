"""Strict API and AI structured-output schemas."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReceiptExtraction(StrictModel):
    is_valid_receipt_or_screenshot: bool
    image_quality_issue: str | None = None
    merchant_name: str | None = None
    merchant_name_quote: str | None = None
    claim_amount_inr: float | None = Field(default=None, ge=0, le=100_000_000)
    claim_amount_quote: str | None = None
    transaction_date: str | None = None
    transaction_date_quote: str | None = None
    utr_or_order_id: str | None = None
    utr_or_order_id_quote: str | None = None
    payment_status_shown: str | None = None


class EvidenceExtraction(StrictModel):
    detected_language: str = "en"
    merchant_name: str | None = None
    merchant_quote: str | None = None
    claim_amount_inr: float | None = Field(default=None, ge=0, le=100_000_000)
    amount_quote: str | None = None
    transaction_date: str | None = None
    date_quote: str | None = None
    utr_or_order_id: str | None = None
    utr_quote: str | None = None
    grievance_description: str | None = None
    grievance_quote: str | None = None


class CaseResponse(StrictModel):
    case_reference: str
    status: str
    detected_language: str
    extracted_facts: dict
    missing_fields: list[str]
    follow_up_question_native: str | None = None
    draft_summary_native: str | None = None
    legal_draft_en: str | None = None
    nodal_email_payload: dict | None = None
    audit_log: list[dict] = Field(default_factory=list)
    delivery_status: str | None = None
    pdf_url: str | None = None
    pdf_size_bytes: int | None = None
    message: str | None = None


class RespondBody(StrictModel):
    text: str | None = Field(default=None, max_length=12000)
    preferred_language: str | None = Field(default=None, max_length=20)

    @field_validator("text")
    @classmethod
    def require_text_or_upload(cls, value):
        return value


class ApproveBody(StrictModel):
    approved: Literal[True]


class DocketBody(StrictModel):
    nch_docket_number: str = Field(min_length=2, max_length=120)


class PurgeResponse(StrictModel):
    case_reference: str
    status: str
    erased_objects: int


class ReadinessResponse(StrictModel):
    status: str
    checks: dict
