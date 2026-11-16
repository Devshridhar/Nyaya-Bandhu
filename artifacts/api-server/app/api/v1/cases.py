"""Authenticated case intake, review, approval, PDF, docket, and purge endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal, session_scope
from app.core.rate_limit import enforce_user_hourly_limit, limiter
from app.core.security import (
    blind_index,
    contact_index,
    create_pdf_token,
    encrypt_bytes,
    encrypt_text,
    new_case_key,
    require_api_key,
    verify_pdf_token,
    wrap_case_key,
)
from app.models.domain import AuditLog, CaseStatus, GrievanceCase, IdempotencyRecord, NodalDirectory
from app.schemas.payloads import ApproveBody, DocketBody
from app.services.case_service import (
    add_audit,
    add_message,
    case_key,
    complete_idempotency,
    decrypt_field,
    deserialize_case,
    encrypted_object,
    get_case_by_reference,
    make_expiration,
    purge_case_data,
    reserve_idempotency,
)
from app.services.email_dispatcher import dispatch_nodal_email
from app.services.evidence_service import sanitize_image, validate_audio
from app.services.legal_agent import (
    FIELD_LABELS,
    build_follow_up,
    build_legal_draft,
    build_native_summary,
    build_nodal_email,
    evaluate_completeness,
    extract_text_facts,
    facts_for_response,
    merge_facts,
    safe_json,
)
from app.services.nodal_directory import directory_public_rows
from app.services.pdf_bundler import MAX_PDF_BYTES, build_pdf_bundle
from app.services.speech_service import transcribe_audio
from app.services.storage_service import storage
from app.services.vision_service import extract_receipt

logger = logging.getLogger("nyaya.api.cases")
router = APIRouter(prefix="/cases", tags=["cases"])
MAX_SCREENSHOTS = 3


def _error_for_input(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _create_reference() -> str:
    return f"NB-{datetime.now(timezone.utc).year}-{uuid.uuid4().hex[:6].upper()}"


def _get_case_sync(case_reference: str) -> dict:
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found.")
        return deserialize_case(session, case)


def _load_case_inputs(case_reference: str) -> dict:
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found.")
        if not case.wrapped_case_key or case.status == CaseStatus.PURGED.value:
            raise HTTPException(status_code=410, detail="This case has been purged.")
        facts = json.loads(decrypt_field(case, "facts", case.encrypted_facts_json) or "{}")
        transcript = decrypt_field(case, "transcript", case.encrypted_transcript) or ""
        ocr = json.loads(decrypt_field(case, "ocr", case.encrypted_ocr_json) or "[]")
        if isinstance(ocr, dict):
            ocr = ocr.get("items", [])
        email = case.nodal_email_payload
        payload = {
            "id": case.id,
            "case_reference": case.case_reference,
            "status": case.status,
            "facts": facts,
            "transcript": transcript,
            "ocr": ocr,
            "evidence_keys": list(case.evidence_keys or []),
            "pdf_bundle_path": case.pdf_bundle_path,
            "pdf_size_bytes": case.pdf_size_bytes,
            "detected_language": case.detected_language,
            "follow_up_count": case.follow_up_count,
        "user_contact_hash": case.user_contact_hash,
            "expires_at": case.expires_at.isoformat(),
        }
        if email and email.get("ciphertext"):
            from app.services.case_service import decrypted_object
            payload["email"] = decrypted_object(case, "nodal_email_payload", email)
        return payload


async def _process_evidence(
    *,
    text: str | None,
    audio_bytes: bytes | None,
    audio_filename: str,
    screenshots: list[tuple[bytes, str]],
    preferred_language: str | None,
) -> tuple[str, str, list[dict], list[bytes]]:
    combined_text = (text or "").strip()
    language = preferred_language or "en"
    if audio_bytes:
        try:
            transcription = await transcribe_audio(audio_bytes, audio_filename, preferred_language)
        except Exception as exc:
            logger.exception("transcription_failed", extra={"event": "speech_error"})
            raise HTTPException(status_code=502, detail="Voice transcription failed. Please retry or provide text.") from exc
        if transcription:
            combined_text = "\n".join(filter(None, [combined_text, transcription]))

    sanitized_images = []
    ocr_results = []
    for image_data, filename in screenshots:
        try:
            sanitized = await asyncio.to_thread(sanitize_image, image_data, filename)
        except ValueError as exc:
            raise _error_for_input(exc) from exc
        sanitized_images.append(sanitized.data)
        try:
            ocr = await extract_receipt(sanitized.data)
        except Exception as exc:
            logger.exception("receipt_extraction_failed", extra={"event": "vision_error"})
            raise HTTPException(status_code=502, detail="Screenshot processing failed. Please retry.") from exc
        if not ocr.is_valid_receipt_or_screenshot:
            detail = ocr.image_quality_issue or "The image does not appear to be payment or invoice evidence."
            raise HTTPException(status_code=422, detail=f"Screenshot not accepted: {detail}")
        if ocr.image_quality_issue:
            raise HTTPException(status_code=422, detail=f"Screenshot is not clear enough: {ocr.image_quality_issue}")
        ocr_results.append(ocr.model_dump())
    if not combined_text and not ocr_results:
        raise HTTPException(status_code=422, detail="Provide text, a voice note, or at least one screenshot.")
    return combined_text, language, ocr_results, sanitized_images


def _ocr_facts(ocr_rows: list[dict]) -> dict:
    combined: dict[str, Any] = {}
    conflicts: dict[str, list] = {}
    for item in ocr_rows:
        row_facts = {}
        for field, quote_field in (
            ("merchant_name", "merchant_name_quote"),
            ("claim_amount_inr", "claim_amount_quote"),
            ("transaction_date", "transaction_date_quote"),
            ("utr_or_order_id", "utr_or_order_id_quote"),
        ):
            value = item.get(field)
            quote = item.get(quote_field)
            if value is None or not isinstance(quote, str):
                continue
            if field == "merchant_name" and str(value).casefold() not in quote.casefold():
                continue
            if field == "claim_amount_inr":
                quoted_amounts = re.findall(r"\d[\d,]*(?:\.\d+)?", quote)
                if not any(abs(float(part.replace(",", "")) - float(value)) < 0.005 for part in quoted_amounts):
                    continue
            if field == "utr_or_order_id" and str(value).casefold() not in quote.casefold():
                continue
            if field == "transaction_date":
                parsed_value = None
                parsed_quote = None
                from app.services.legal_agent import _DATE_PATTERNS
                for pattern in _DATE_PATTERNS:
                    try:
                        parsed_value = datetime.strptime(str(value), pattern)
                        break
                    except ValueError:
                        continue
                for pattern in _DATE_PATTERNS:
                    try:
                        parsed_quote = datetime.strptime(quote, pattern)
                        break
                    except ValueError:
                        continue
                if not parsed_value or not parsed_quote or parsed_value.date() != parsed_quote.date():
                    continue
                value = parsed_value.strftime("%d/%m/%Y")
            row_facts[field] = value
        combined, conflicts = merge_facts(combined, row_facts, user_response=False)
    combined["_conflicts"] = conflicts
    return combined


async def _extract_and_merge(
    text: str,
    ocr_rows: list[dict],
    existing_facts: dict | None = None,
    language: str | None = None,
    user_response: bool = False,
) -> tuple[dict, dict]:
    text_facts = {}
    if text.strip():
        try:
            text_facts = await extract_text_facts(text, language)
        except Exception as exc:
            logger.exception("text_fact_extraction_failed", extra={"event": "legal_extraction_error"})
            raise HTTPException(status_code=502, detail="Could not safely extract complaint facts. Please retry.") from exc
    facts, conflicts = merge_facts(existing_facts, text_facts, user_response=user_response)
    ocr = _ocr_facts(ocr_rows)
    # Within one response, typed correction takes precedence over older evidence,
    # while an image that contradicts that typed statement is still flagged.
    facts, conflicts = merge_facts(facts, ocr, user_response=False)
    if language:
        facts["detected_language"] = text_facts.get("detected_language", language)
    facts["_conflicts"] = conflicts
    return facts, conflicts


def _persist_new_case(
    user_contact: str,
    contact_hash: str,
    text: str,
    ocr_rows: list[dict],
    facts: dict,
    conflicts: dict,
    language: str,
    sanitized_images: list[bytes],
    idempotency_key: str | None,
) -> dict:
    case_id = str(uuid.uuid4())
    case_key_bytes = new_case_key()
    reference = _create_reference()
    missing = evaluate_completeness(facts, conflicts)
    status = CaseStatus.AWAITING_MISSING_INFO.value if missing else CaseStatus.READY_FOR_USER_REVIEW.value
    case = GrievanceCase(
        id=case_id,
        case_reference=reference,
        user_contact_hash=contact_hash,
        encrypted_user_contact=encrypt_text(case_key_bytes, user_contact, f"{case_id}:user_contact"),
        wrapped_case_key=wrap_case_key(case_id, case_key_bytes),
        status=CaseStatus.INTAKE_RECEIVED.value,
        detected_language=language,
        missing_fields=[],
        evidence_keys=[],
        expires_at=make_expiration(),
    )
    uploaded = []
    try:
        with session_scope() as session:
            session.add(case)
            session.flush()
            transcript_value = encrypt_text(case_key_bytes, text, f"{case_id}:transcript") if text else None
            ocr_value = encrypt_text(case_key_bytes, safe_json({"items": ocr_rows}), f"{case_id}:ocr")
            facts_value = encrypt_text(case_key_bytes, safe_json(facts), f"{case_id}:facts")
            utr = facts.get("utr_or_order_id")
            utr_hash = blind_index("utr", str(utr)) if utr else None
            if utr_hash and session.scalar(
                select(GrievanceCase.case_reference).where(
                    GrievanceCase.utr_or_invoice_hash == utr_hash,
                    GrievanceCase.status != CaseStatus.PURGED.value,
                )
            ):
                raise HTTPException(status_code=409, detail="A case already exists for this transaction reference.")
            case.encrypted_transcript = transcript_value
            case.encrypted_ocr_json = ocr_value
            case.encrypted_facts_json = facts_value
            case.utr_or_invoice_hash = utr_hash
            case.missing_fields = missing
            if missing:
                case.follow_up_question_native = encrypt_text(
                    case_key_bytes,
                    build_follow_up(missing, conflicts, language),
                    f"{case_id}:follow_up",
                )
            else:
                case.legal_draft_en = encrypt_text(case_key_bytes, build_legal_draft(reference, facts), f"{case_id}:legal_draft")
                case.draft_summary_native = encrypt_text(case_key_bytes, build_native_summary(facts, reference, language), f"{case_id}:summary")
                email = _build_email_in_session(session, reference, facts)
                case.nodal_email_payload = {"ciphertext": encrypt_text(case_key_bytes, json.dumps(email, ensure_ascii=False), f"{case_id}:nodal_email_payload")}
            previous = case.status
            case.status = status
            add_audit(session, case, previous, status, "system", {"missing_fields": missing})
            if text:
                add_message(session, case, "user", text, "api")
            for index, image in enumerate(sanitized_images):
                object_name = f"cases/{reference}/evidence_{index + 1}.jpg"
                encrypted_image = encrypt_bytes(case_key_bytes, image, f"{case_id}:evidence:{index + 1}")
                storage.upload(object_name, encrypted_image)
                uploaded.append(object_name)
                case.evidence_keys = list(case.evidence_keys or []) + [object_name]
            if idempotency_key:
                complete_idempotency(session, idempotency_key, "intake", reference)
        return _get_case_sync(reference)
    except Exception:
        for name in uploaded:
            try:
                storage.delete(name)
            except Exception:
                logger.exception("failed_to_clean_uncommitted_object", extra={"event": "storage_cleanup"})
        raise


def _build_email_in_session(session: Session, reference: str, facts: dict) -> dict:
    return build_nodal_email(session, reference, facts, build_legal_draft(reference, facts))


def _save_response(
    case_reference: str,
    text: str,
    ocr_rows: list[dict],
    facts: dict,
    conflicts: dict,
    language: str,
    sanitized_images: list[bytes],
    idempotency_key: str | None,
    source: str = "api",
) -> dict:
    uploaded = []
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found.")
        if not case.wrapped_case_key or case.status == CaseStatus.PURGED.value:
            raise HTTPException(status_code=410, detail="This case has been purged.")
        if case.status == CaseStatus.FILED_COMPLETE.value:
            raise HTTPException(status_code=409, detail="This complaint has already been sent. Start a new case for another issue.")
        old_status = case.status
        key = case_key(case)
        previous_transcript = decrypt_field(case, "transcript", case.encrypted_transcript) or ""
        previous_ocr = json.loads(decrypt_field(case, "ocr", case.encrypted_ocr_json) or "[]")
        if isinstance(previous_ocr, dict):
            previous_ocr = previous_ocr.get("items", [])
        # Recompute from accumulated evidence; new user statements can explicitly correct
        # a value previously flagged as conflicting.
        # _extract_and_merge already combined the new turn with stored facts and
        # preserved any same-turn contradictions. Do not merge a second time.
        facts = dict(facts)
        conflicts = dict(conflicts)
        all_ocr = previous_ocr + ocr_rows
        facts["_conflicts"] = conflicts
        missing = evaluate_completeness(facts, conflicts)
        case.detected_language = language or facts.get("detected_language", case.detected_language)
        case.missing_fields = missing
        case.encrypted_transcript = encrypt_text(key, "\n".join(filter(None, [previous_transcript, text])), f"{case.id}:transcript")
        case.encrypted_ocr_json = encrypt_text(key, safe_json({"items": all_ocr}), f"{case.id}:ocr")
        case.encrypted_facts_json = encrypt_text(key, safe_json(facts), f"{case.id}:facts")
        utr = facts.get("utr_or_order_id")
        utr_hash = blind_index("utr", str(utr)) if utr else None
        if utr_hash and session.scalar(
            select(GrievanceCase.case_reference).where(
                GrievanceCase.utr_or_invoice_hash == utr_hash,
                GrievanceCase.id != case.id,
                GrievanceCase.status != CaseStatus.PURGED.value,
            )
        ):
            raise HTTPException(status_code=409, detail="A case already exists for this transaction reference.")
        case.utr_or_invoice_hash = utr_hash
        had_pending_follow_up = case.follow_up_question_native is not None
        if missing:
            if case.follow_up_count < 5 and (
                (old_status == CaseStatus.AWAITING_MISSING_INFO.value and had_pending_follow_up)
                or old_status != CaseStatus.AWAITING_MISSING_INFO.value
            ):
                case.follow_up_count += 1
            case.status = CaseStatus.AWAITING_MISSING_INFO.value
            if case.follow_up_count < 5:
                question = build_follow_up(missing, conflicts, case.detected_language)
                case.follow_up_question_native = encrypt_text(key, question, f"{case.id}:follow_up")
            else:
                case.follow_up_question_native = None
        else:
            case.status = CaseStatus.READY_FOR_USER_REVIEW.value
            case.follow_up_question_native = None
            draft = build_legal_draft(case_reference, facts)
            case.legal_draft_en = encrypt_text(key, draft, f"{case.id}:legal_draft")
            case.draft_summary_native = encrypt_text(
                key,
                build_native_summary(facts, case_reference, case.detected_language),
                f"{case.id}:summary",
            )
            case.nodal_email_payload = encrypted_object(case, "nodal_email_payload", build_nodal_email(session, case_reference, facts, draft))
        for index, image in enumerate(sanitized_images, start=len(case.evidence_keys or []) + 1):
            object_name = f"cases/{case_reference}/evidence_{index}.jpg"
            storage.upload(object_name, encrypt_bytes(key, image, f"{case.id}:evidence:{index}"))
            uploaded.append(object_name)
            case.evidence_keys = list(case.evidence_keys or []) + [object_name]
        add_message(session, case, "user", text or "[image or voice evidence]", source)
        add_audit(session, case, old_status, case.status, "user", {"missing_fields": missing, "follow_up_count": case.follow_up_count})
        if idempotency_key:
            complete_idempotency(session, idempotency_key, f"respond:{case_reference}", case_reference)
    try:
        return _get_case_sync(case_reference)
    except Exception:
        # If a DB commit did not complete, avoid leaving orphaned newly-uploaded items.
        for name in uploaded:
            storage.delete(name)
        raise


@router.post("/intake")
@limiter.limit("10/hour")
async def intake(
    request: Request,
    background_tasks: BackgroundTasks,
    audio: UploadFile | None = File(default=None),
    text: str | None = Form(default=None, max_length=12000),
    screenshots: list[UploadFile] = File(default=[]),
    user_contact: str = Form(..., min_length=8, max_length=64),
    preferred_language: str | None = Form(default=None, max_length=20),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_api_key),
):
    if len(screenshots) > MAX_SCREENSHOTS:
        raise HTTPException(status_code=413, detail="A request can include at most 3 screenshots.")
    try:
        contact_hash = contact_index(user_contact)
    except ValueError as exc:
        raise _error_for_input(exc) from exc
    enforce_user_hourly_limit(contact_hash)

    reserved_key = None
    if idempotency_key:
        try:
            state, value = await run_in_threadpool(reserve_idempotency, SessionLocal, idempotency_key, "intake")
        except (ValueError, Exception) as exc:
            if isinstance(exc, ValueError):
                raise _error_for_input(exc) from exc
            logger.exception("idempotency_reservation_failed")
            raise HTTPException(status_code=409, detail="Could not reserve this request. Retry with a new idempotency key.") from exc
        if state == "replay":
            return await run_in_threadpool(_get_case_sync, value)
        if state == "busy":
            raise HTTPException(status_code=409, detail="A request with this Idempotency-Key is already being processed.")
        reserved_key = value

    try:
        audio_bytes = await audio.read() if audio else None
        audio_filename = audio.filename if audio else "voice-note.webm"
        if audio_bytes:
            try:
                validate_audio(audio_bytes, audio_filename or "")
            except ValueError as exc:
                raise _error_for_input(exc) from exc
        screenshot_bytes = []
        for item in screenshots:
            data = await item.read()
            if not data:
                raise HTTPException(status_code=422, detail="An uploaded screenshot is empty.")
            if len(data) > get_settings().max_upload_bytes:
                raise HTTPException(status_code=413, detail="Each screenshot must be no larger than 10 MB.")
            screenshot_bytes.append((data, item.filename or "screenshot.jpg"))
        combined_text, language, ocr_rows, sanitized_images = await _process_evidence(
            text=text,
            audio_bytes=audio_bytes,
            audio_filename=audio_filename or "voice-note.webm",
            screenshots=screenshot_bytes,
            preferred_language=preferred_language,
        )
        facts, conflicts = await _extract_and_merge(combined_text, ocr_rows, language=language)
        language = facts.get("detected_language") or language
        result = await run_in_threadpool(
            _persist_new_case,
            user_contact,
            contact_hash,
            combined_text,
            ocr_rows,
            facts,
            conflicts,
            language,
            sanitized_images,
            idempotency_key,
        )
        return result
    except Exception:
        if reserved_key:
            await run_in_threadpool(_abandon_reservation, reserved_key)
        raise


def _abandon_reservation(key: str) -> None:
    with session_scope() as session:
        record = session.get(IdempotencyRecord, key)
        if record and not record.case_reference:
            session.delete(record)


async def _parse_respond_request(request: Request):
    content_type = request.headers.get("content-type", "")
    text = None
    language = None
    audio_bytes = None
    audio_filename = "voice-note.webm"
    images = []
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        text = str(form.get("text", "") or "")
        language = str(form.get("preferred_language", "") or "") or None
        audio = form.get("audio")
        if audio and hasattr(audio, "read"):
            audio_bytes = await audio.read()
            audio_filename = audio.filename or audio_filename
        for item in form.getlist("screenshots"):
            if hasattr(item, "read"):
                images.append((await item.read(), item.filename or "screenshot.jpg"))
    elif "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON request body.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Request body must be an object.")
        if set(body) - {"text", "preferred_language"}:
            raise HTTPException(status_code=422, detail="Only text and preferred_language are accepted.")
        text = body.get("text")
        language = body.get("preferred_language")
    else:
        body = await request.body()
        text = body.decode("utf-8", "replace") if body else None
    if text is not None and (not isinstance(text, str) or len(text) > 12000):
        raise HTTPException(status_code=422, detail="Text must be at most 12,000 characters.")
    if language is not None and (not isinstance(language, str) or len(language) > 20):
        raise HTTPException(status_code=422, detail="Language code is too long.")
    if len(images) > MAX_SCREENSHOTS:
        raise HTTPException(status_code=413, detail="A request can include at most 3 screenshots.")
    for data, _filename in images:
        if not data:
            raise HTTPException(status_code=422, detail="An uploaded screenshot is empty.")
        if len(data) > get_settings().max_upload_bytes:
            raise HTTPException(status_code=413, detail="Each screenshot must be no larger than 10 MB.")
    if audio_bytes:
        if len(audio_bytes) > get_settings().max_upload_bytes:
            raise HTTPException(status_code=413, detail="Audio must be no larger than 10 MB.")
        try:
            validate_audio(audio_bytes, audio_filename)
        except ValueError as exc:
            raise _error_for_input(exc) from exc
    if not (text or "").strip() and not audio_bytes and not images:
        raise HTTPException(status_code=422, detail="Provide text, a voice note, or at least one screenshot.")
    return text or "", language, audio_bytes, audio_filename, images


@router.post("/{case_reference}/respond")
@limiter.limit("10/hour")
async def respond(
    case_reference: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_api_key),
):
    reserved_key = None
    operation = f"respond:{case_reference}"
    if idempotency_key:
        state, value = await run_in_threadpool(reserve_idempotency, SessionLocal, idempotency_key, operation)
        if state == "replay":
            return await run_in_threadpool(_get_case_sync, value)
        if state == "busy":
            raise HTTPException(status_code=409, detail="A request with this Idempotency-Key is already being processed.")
        reserved_key = value
    try:
        existing = await run_in_threadpool(_load_case_inputs, case_reference)
        if existing.get("user_contact_hash"):
            enforce_user_hourly_limit(existing["user_contact_hash"])
        text, language, audio_bytes, audio_filename, images = await _parse_respond_request(request)
        if audio_bytes:
            try:
                transcription = await transcribe_audio(audio_bytes, audio_filename, language or existing["detected_language"])
            except Exception as exc:
                logger.exception("response_transcription_failed", extra={"case_reference": case_reference})
                raise HTTPException(status_code=502, detail="Voice transcription failed. Please retry or provide text.") from exc
            text = "\n".join(filter(None, [text, transcription]))
        combined, detected_language, ocr_rows, sanitized = await _process_evidence(
            text=text,
            audio_bytes=None,
            audio_filename=audio_filename,
            screenshots=images,
            preferred_language=language or existing["detected_language"],
        ) if images else (text.strip(), language or existing["detected_language"], [], [])
        incoming, conflicts = await _extract_and_merge(
            combined,
            ocr_rows,
            existing_facts=existing["facts"],
            language=detected_language,
            user_response=True,
        )
        detected_language = incoming.get("detected_language") or detected_language
        result = await run_in_threadpool(
            _save_response,
            case_reference,
            combined,
            ocr_rows,
            incoming,
            conflicts,
            detected_language,
            sanitized,
            idempotency_key,
        )
        return result
    except Exception:
        if reserved_key:
            await run_in_threadpool(_abandon_reservation, reserved_key)
        raise


@router.get("/{case_reference}")
async def get_case(case_reference: str, _: None = Depends(require_api_key)):
    result = await run_in_threadpool(_get_case_sync, case_reference)
    payload = None
    if result["status"] != CaseStatus.PURGED.value:
        payload = await run_in_threadpool(_load_case_inputs, case_reference)
    if payload and payload.get("pdf_bundle_path"):
        result["pdf_url"] = f"/api/v1/cases/{case_reference}/pdf?token={create_pdf_token(case_reference)}"
    return result


async def _approve_case(case_reference: str, idempotency_key: str | None = None) -> dict:
    inputs = await run_in_threadpool(_prepare_approval, case_reference)
    if inputs.get("already_filed"):
        return inputs["response"]
    key = inputs["case_key"]
    pdf_bytes = None
    if inputs["pdf_path"]:
        try:
            encrypted = await run_in_threadpool(storage.download, inputs["pdf_path"])
            from app.core.security import decrypt_bytes
            pdf_bytes = await asyncio.to_thread(decrypt_bytes, key, encrypted, f"{inputs['id']}:pdf")
        except Exception:
            logger.warning("existing_pdf_not_readable_will_rebuild", extra={"case_reference": case_reference})
    if pdf_bytes is None:
        evidence_bytes = []
        from app.core.security import decrypt_bytes, encrypt_bytes
        for index, object_name in enumerate(inputs["evidence_keys"], start=1):
            encrypted_image = await run_in_threadpool(storage.download, object_name)
            evidence_bytes.append(await asyncio.to_thread(decrypt_bytes, key, encrypted_image, f"{inputs['id']}:evidence:{index}"))
        pdf_bytes = await asyncio.to_thread(
            build_pdf_bundle,
            case_reference,
            facts_for_response(inputs["facts"]),
            inputs["draft"],
            evidence_bytes,
        )
        if len(pdf_bytes) >= MAX_PDF_BYTES:
            raise HTTPException(status_code=500, detail="Generated PDF exceeded the 2 MB upload limit.")
        pdf_path = f"cases/{case_reference}/Nyaya_Bandhu_Bundle.pdf"
        encrypted_pdf = await asyncio.to_thread(encrypt_bytes, key, pdf_bytes, f"{inputs['id']}:pdf")
        await run_in_threadpool(storage.upload, pdf_path, encrypted_pdf)
        await run_in_threadpool(_record_pdf, case_reference, pdf_path, len(pdf_bytes))

    try:
        email_result = await dispatch_nodal_email(inputs["email"] or {}, pdf_bytes, case_reference)
    except Exception as exc:
        logger.exception("nodal_email_dispatch_failed", extra={"case_reference": case_reference})
        raise HTTPException(status_code=502, detail="The PDF is ready, but the grievance email could not be sent. Retry approval.") from exc
    result = await run_in_threadpool(_finalize_approval, case_reference, email_result, idempotency_key)
    result["pdf_url"] = f"/api/v1/cases/{case_reference}/pdf?token={create_pdf_token(case_reference)}"
    result["pdf_size_bytes"] = len(pdf_bytes)
    result["message"] = (
        "Complaint email sent to the verified merchant address."
        if email_result["status"] == "SENT"
        else "PDF is ready. No merchant email was sent; use the National Consumer Helpline filing options below."
    )
    result["nch_filing"] = {
        "phone": "1915",
        "whatsapp": "8800001915",
        "website": "https://consumerhelpline.gov.in/",
        "steps": ["Download the PDF bundle.", "File at the National Consumer Helpline or consumerhelpline.gov.in.", "Save and record the official docket number."],
    }
    return result


def _prepare_approval(case_reference: str) -> dict:
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found.")
        if case.status == CaseStatus.PURGED.value:
            raise HTTPException(status_code=410, detail="This case has been purged.")
        if case.status == CaseStatus.FILED_COMPLETE.value:
            return {"already_filed": True, "response": deserialize_case(session, case, "This complaint was already sent.")}
        if case.status not in (CaseStatus.READY_FOR_USER_REVIEW.value, CaseStatus.APPROVED_AND_FILING.value):
            raise HTTPException(status_code=409, detail="The case is not ready for approval.")
        previous = case.status
        if case.status != CaseStatus.APPROVED_AND_FILING.value:
            case.status = CaseStatus.APPROVED_AND_FILING.value
            add_audit(session, case, previous, case.status, "user", {"approval": True})
        session.flush()
        inputs = {
            "id": case.id,
            "reference": case.case_reference,
            "facts": json.loads(decrypt_field(case, "facts", case.encrypted_facts_json) or "{}"),
            "draft": decrypt_field(case, "legal_draft", case.legal_draft_en) or "",
            "evidence_keys": list(case.evidence_keys or []),
            "pdf_path": case.pdf_bundle_path,
            "pdf_size": case.pdf_size_bytes,
            "email": None,
            "case_key": case_key(case),
        }
        if case.nodal_email_payload:
            from app.services.case_service import decrypted_object
            inputs["email"] = decrypted_object(case, "nodal_email_payload", case.nodal_email_payload)
    return inputs


def _record_pdf(case_reference: str, pdf_path: str, size: int) -> None:
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found.")
        case.pdf_bundle_path = pdf_path
        case.pdf_size_bytes = size
        add_audit(session, case, case.status, case.status, "system", {"pdf_size_bytes": size})


def _finalize_approval(case_reference: str, email_result: dict, idempotency_key: str | None) -> dict:
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case or case.status == CaseStatus.PURGED.value:
            raise HTTPException(status_code=410, detail="This case has been purged.")
        previous = case.status
        case.delivery_status = email_result["status"]
        if email_result["status"] == "SENT":
            case.status = CaseStatus.FILED_COMPLETE.value
        add_audit(
            session,
            case,
            previous,
            case.status,
            "email_dispatcher",
            {"delivery_status": email_result["status"]},
        )
        if idempotency_key:
            complete_idempotency(session, idempotency_key, f"approve:{case_reference}", case_reference)
        return deserialize_case(session, case)


@router.post("/{case_reference}/approve")
@limiter.limit("10/hour")
async def approve(
    case_reference: str,
    request: Request,
    body: ApproveBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_api_key),
):
    reserved_key = None
    operation = f"approve:{case_reference}"
    if idempotency_key:
        state, value = await run_in_threadpool(reserve_idempotency, SessionLocal, idempotency_key, operation)
        if state == "replay":
            result = await run_in_threadpool(_get_case_sync, value)
            payload = await run_in_threadpool(_load_case_inputs, case_reference)
            if payload.get("pdf_bundle_path"):
                result["pdf_url"] = f"/api/v1/cases/{case_reference}/pdf?token={create_pdf_token(case_reference)}"
            return result
        if state == "busy":
            raise HTTPException(status_code=409, detail="A request with this Idempotency-Key is already being processed.")
        reserved_key = value
    try:
        return await _approve_case(case_reference, idempotency_key)
    except Exception:
        if reserved_key:
            await run_in_threadpool(_abandon_reservation, reserved_key)
        raise


@router.get("/{case_reference}/pdf")
async def download_pdf(
    case_reference: str,
    request: Request,
    token: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    from app.core.security import constant_time_equal

    if not verify_pdf_token(case_reference, token or ""):
        if not get_settings().admin_api_key or not constant_time_equal(x_api_key, get_settings().admin_api_key):
            raise HTTPException(status_code=401, detail="A valid API key or signed PDF link is required.")
    payload = await run_in_threadpool(_load_case_inputs, case_reference)
    if not payload.get("pdf_bundle_path"):
        raise HTTPException(status_code=404, detail="PDF has not been generated yet.")
    encrypted = await run_in_threadpool(storage.download, payload["pdf_bundle_path"])
    from app.core.security import decrypt_bytes
    pdf_bytes = await asyncio.to_thread(decrypt_bytes, case_key_from_payload(case_reference), encrypted, f"{payload['id']}:pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{case_reference}_Nyaya_Bandhu.pdf"', "Cache-Control": "private, no-store"},
    )


def case_key_from_payload(case_reference: str) -> bytes:
    with session_scope() as session:
        case = get_case_by_reference(session, case_reference)
        if not case or not case.wrapped_case_key:
            raise HTTPException(status_code=410, detail="This case has been purged.")
        return case_key(case)


@router.patch("/{case_reference}/docket")
async def add_docket(
    case_reference: str,
    body: DocketBody,
    _: None = Depends(require_api_key),
):
    docket = body.nch_docket_number.strip()
    if len(docket) < 2 or not re.fullmatch(r"[\w./-]+", docket, flags=re.UNICODE):
        raise HTTPException(status_code=422, detail="Docket number contains unsupported characters.")
    def _save():
        with session_scope() as session:
            case = get_case_by_reference(session, case_reference)
            if not case:
                raise HTTPException(status_code=404, detail="Case not found.")
            if case.status == CaseStatus.PURGED.value:
                raise HTTPException(status_code=410, detail="This case has been purged.")
            case.nch_docket_number = docket
            add_audit(session, case, case.status, case.status, "user", {"docket_recorded": True})
            return deserialize_case(session, case)
    return await run_in_threadpool(_save)


@router.delete("/{case_reference}/purge")
async def purge_case(case_reference: str, _: None = Depends(require_api_key)):
    def _purge():
        with session_scope() as session:
            case = get_case_by_reference(session, case_reference)
            if not case:
                raise HTTPException(status_code=404, detail="Case not found.")
            if case.status == CaseStatus.PURGED.value:
                return {"case_reference": case_reference, "status": "PURGED", "erased_objects": 0}
            erased = purge_case_data(session, case, "user")
            return {"case_reference": case_reference, "status": case.status, "erased_objects": erased}
    return await run_in_threadpool(_purge)


@router.get("/directory/nodal")
async def nodal_directory(_: None = Depends(require_api_key)):
    def _list():
        with session_scope() as session:
            return directory_public_rows(session)
    return {"merchants": await run_in_threadpool(_list)}
