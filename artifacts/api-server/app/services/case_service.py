"""Case persistence helpers, encrypted serialization, audit, and crypto-purge."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import decrypt_text, encrypt_text, unwrap_case_key
from app.models.domain import AuditLog, CaseMessage, GrievanceCase, IdempotencyRecord
from app.services.storage_service import storage

logger = logging.getLogger("nyaya.case")


def case_key(case: GrievanceCase) -> bytes:
    if not case.wrapped_case_key:
        raise ValueError("This case has been cryptographically purged.")
    return unwrap_case_key(case.id, case.wrapped_case_key)


def encrypt_field(case: GrievanceCase, name: str, value: str) -> str:
    return encrypt_text(case_key(case), value, f"{case.id}:{name}")


def decrypt_field(case: GrievanceCase, name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return decrypt_text(case_key(case), value, f"{case.id}:{name}")


def encrypted_object(case: GrievanceCase, name: str, value: dict) -> dict:
    return {"ciphertext": encrypt_field(case, name, json.dumps(value, ensure_ascii=False, separators=(",", ":")))}


def decrypted_object(case: GrievanceCase, name: str, value: dict | None) -> dict | None:
    if not value:
        return None
    ciphertext = value.get("ciphertext")
    if not ciphertext:
        return None
    return json.loads(decrypt_field(case, name, ciphertext) or "{}")


def add_audit(
    session: Session,
    case: GrievanceCase,
    previous_status: str | None,
    new_status: str,
    actor: str,
    metadata: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            case_id=case.id,
            previous_status=previous_status,
            new_status=new_status,
            actor=actor[:32],
            metadata_json=metadata or {},
        )
    )


def add_message(session: Session, case: GrievanceCase, role: str, content: str, source: str = "api") -> None:
    session.add(
        CaseMessage(
            case_id=case.id,
            role=role,
            encrypted_content=encrypt_field(case, "message", content),
            source=source[:20],
        )
    )


def get_case_by_reference(session: Session, case_reference: str) -> GrievanceCase | None:
    return session.scalar(select(GrievanceCase).where(GrievanceCase.case_reference == case_reference))


def deserialize_case(session: Session, case: GrievanceCase, message: str | None = None) -> dict:
    purged = case.status == "PURGED" or not case.wrapped_case_key
    facts = {}
    transcript = None
    ocr = []
    summary = None
    draft = None
    follow_up = None
    email = None
    if not purged:
        try:
            facts = json.loads(decrypt_field(case, "facts", case.encrypted_facts_json) or "{}")
            transcript = decrypt_field(case, "transcript", case.encrypted_transcript)
            ocr = json.loads(decrypt_field(case, "ocr", case.encrypted_ocr_json) or "[]")
            summary = decrypt_field(case, "summary", case.draft_summary_native)
            draft = decrypt_field(case, "legal_draft", case.legal_draft_en)
            follow_up = decrypt_field(case, "follow_up", case.follow_up_question_native)
            email = decrypted_object(case, "nodal_email_payload", case.nodal_email_payload)
        except Exception:
            logger.exception("case_decryption_failed", extra={"case_reference": case.case_reference})
            raise
    audits = session.scalars(
        select(AuditLog).where(AuditLog.case_id == case.id).order_by(AuditLog.timestamp.asc())
    ).all()
    from app.services.legal_agent import facts_for_response

    return {
        "case_reference": case.case_reference,
        "status": case.status,
        "detected_language": case.detected_language,
        "extracted_facts": facts_for_response(facts),
        "missing_fields": list(case.missing_fields or []),
        "follow_up_question_native": follow_up,
        "draft_summary_native": summary,
        "legal_draft_en": draft,
        "nodal_email_payload": email,
        "audit_log": [
            {
                "previous_status": row.previous_status,
                "new_status": row.new_status,
                "actor": row.actor,
                "timestamp": row.timestamp.isoformat(),
                "metadata": row.metadata_json,
            }
            for row in audits
        ],
        "delivery_status": case.delivery_status,
        "pdf_size_bytes": case.pdf_size_bytes,
        "message": message,
    }


def reserve_idempotency(session_factory, key: str | None, operation: str):
    """Reserve a request key before expensive AI work; return ('new'|'replay'|'busy', ref)."""
    if not key:
        return "new", None
    clean = key.strip()
    if not clean or len(clean) > 200:
        raise ValueError("Idempotency-Key must contain 1 to 200 characters.")
    full_key = f"{operation}:{clean}"
    with session_factory() as session:
        existing = session.get(IdempotencyRecord, full_key)
        expiration = existing.expires_at if existing else None
        if expiration and expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        if existing and expiration > datetime.now(timezone.utc):
            if existing.case_reference:
                return "replay", existing.case_reference
            return "busy", None
        if existing:
            session.delete(existing)
            session.flush()
        session.add(
            IdempotencyRecord(
                idempotency_key=full_key,
                operation=operation,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        )
        session.commit()
    return "new", full_key


def complete_idempotency(session: Session, key: str | None, operation: str, case_reference: str) -> None:
    if not key:
        return
    record = session.get(IdempotencyRecord, f"{operation}:{key.strip()}")
    if record:
        record.case_reference = case_reference


def abandon_idempotency(session_factory, full_key: str | None) -> None:
    if not full_key:
        return
    with session_factory() as session:
        record = session.get(IdempotencyRecord, full_key)
        if record and not record.case_reference:
            session.delete(record)


def purge_case_data(session: Session, case: GrievanceCase, actor: str = "user") -> int:
    previous = case.status
    keys = list(case.evidence_keys or [])
    if case.pdf_bundle_path:
        keys.append(case.pdf_bundle_path)
    erased = 0
    for object_key in dict.fromkeys(keys):
        storage.delete(object_key)
        erased += 1
    session.execute(delete(CaseMessage).where(CaseMessage.case_id == case.id))
    case.user_contact_hash = None
    case.encrypted_user_contact = None
    case.wrapped_case_key = None
    case.encrypted_transcript = None
    case.encrypted_ocr_json = None
    case.encrypted_facts_json = None
    case.utr_or_invoice_hash = None
    case.missing_fields = []
    case.follow_up_question_native = None
    case.legal_draft_en = None
    case.draft_summary_native = None
    case.nodal_email_payload = None
    case.evidence_keys = []
    case.pdf_bundle_path = None
    case.pdf_size_bytes = None
    case.nch_docket_number = None
    case.delivery_status = "PURGED"
    case.status = "PURGED"
    case.follow_up_count = 0
    add_audit(session, case, previous, "PURGED", actor, {"objects_deleted": erased})
    return erased


def make_expiration() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=get_settings().data_retention_days)
