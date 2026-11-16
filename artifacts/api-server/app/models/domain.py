"""Persisted domain records; sensitive fields are ciphertext at rest."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaseStatus(str, enum.Enum):
    INTAKE_RECEIVED = "INTAKE_RECEIVED"
    AWAITING_MISSING_INFO = "AWAITING_MISSING_INFO"
    READY_FOR_USER_REVIEW = "READY_FOR_USER_REVIEW"
    APPROVED_AND_FILING = "APPROVED_AND_FILING"
    FILED_COMPLETE = "FILED_COMPLETE"
    PURGED = "PURGED"


class GrievanceCase(Base):
    __tablename__ = "grievance_cases"
    __table_args__ = (
        Index("ix_grievance_contact_status", "user_contact_hash", "status"),
        Index("uq_grievance_utr_hash", "utr_or_invoice_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_reference: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    user_contact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    encrypted_user_contact: Mapped[str | None] = mapped_column(Text, nullable=True)
    wrapped_case_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default=CaseStatus.INTAKE_RECEIVED.value, nullable=False)
    detected_language: Mapped[str] = mapped_column(String(20), default="en", nullable=False)
    encrypted_transcript: Mapped[str | None] = mapped_column(Text)
    encrypted_ocr_json: Mapped[str | None] = mapped_column(Text)
    encrypted_facts_json: Mapped[str | None] = mapped_column(Text)
    utr_or_invoice_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    follow_up_question_native: Mapped[str | None] = mapped_column(Text)
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    legal_draft_en: Mapped[str | None] = mapped_column(Text)
    draft_summary_native: Mapped[str | None] = mapped_column(Text)
    nodal_email_payload: Mapped[dict | None] = mapped_column(JSON)
    delivery_status: Mapped[str | None] = mapped_column(String(32))
    evidence_keys: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    pdf_bundle_path: Mapped[str | None] = mapped_column(String(512))
    pdf_size_bytes: Mapped[int | None] = mapped_column(Integer)
    nch_docket_number: Mapped[str | None] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    messages: Mapped[list["CaseMessage"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    __mapper_args__ = {"version_id_col": version}


class CaseMessage(Base):
    __tablename__ = "case_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(ForeignKey("grievance_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    encrypted_content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="api", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    case: Mapped[GrievanceCase] = relationship(back_populates="messages")


class NodalDirectory(Base):
    __tablename__ = "nodal_directory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    merchant_name: Mapped[str] = mapped_column(String(160), nullable=False)
    sector: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(512))
    turnaround_days: Mapped[int | None] = mapped_column(Integer)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Intentionally not a foreign key: a zero-PII audit trail survives crypto-purge.
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    previous_status: Mapped[str | None] = mapped_column(String(40))
    new_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    case_reference: Mapped[str | None] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
