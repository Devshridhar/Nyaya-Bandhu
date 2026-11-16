"""Twilio WhatsApp webhook endpoint and safe development simulator path."""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import urljoin

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal, session_scope
from app.core.security import contact_index
from app.models.domain import CaseStatus, GrievanceCase, IdempotencyRecord
from app.services.case_service import get_case_by_reference, purge_case_data, reserve_idempotency
from app.services.whatsapp_router import download_twilio_media, send_twilio_reply, verify_twilio_signature

logger = logging.getLogger("nyaya.api.whatsapp")
router = APIRouter(prefix="/webhooks", tags=["whatsapp"])
APPROVE_WORDS = {"yes", "approve", "haan", "हाँ", "हां"}
NEW_WORDS = {"new", "नया"}


def _twiml(message: str = "") -> Response:
    text = f"<Message>{html.escape(message)}</Message>" if message else ""
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{text}</Response>',
        media_type="application/xml",
    )


def _active_case(contact_hash: str):
    with session_scope() as session:
        return session.scalar(
            select(GrievanceCase)
            .where(
                GrievanceCase.user_contact_hash == contact_hash,
                GrievanceCase.status.notin_([CaseStatus.FILED_COMPLETE.value, CaseStatus.PURGED.value]),
            )
            .order_by(GrievanceCase.created_at.desc())
        )


def _mark_message_processed(message_sid: str | None, case_reference: str = "NO_CASE") -> None:
    if not message_sid:
        return
    with session_scope() as session:
        record = session.get(IdempotencyRecord, f"whatsapp:{message_sid}")
        if record:
            record.case_reference = case_reference


def _release_message(message_sid: str | None) -> None:
    if not message_sid:
        return
    with session_scope() as session:
        record = session.get(IdempotencyRecord, f"whatsapp:{message_sid}")
        if record and not record.case_reference:
            session.delete(record)


async def _process_message(
    form: dict,
    media_entries: list[tuple[bytes, str, str]],
    message_sid: str | None,
    base_url: str | None = None,
) -> str:
    from app.api.v1.cases import (
        _approve_case,
        _extract_and_merge,
        _get_case_sync,
        _load_case_inputs,
        _persist_new_case,
        _process_evidence,
        _save_response,
    )
    from app.core.security import contact_index
    from app.models.domain import IdempotencyRecord

    sender = str(form.get("From", ""))
    body = str(form.get("Body", "") or "").strip()
    try:
        contact_hash = contact_index(sender)
    except ValueError:
        await run_in_threadpool(_mark_message_processed, message_sid)
        return "I could not read the WhatsApp phone number. Please contact the operator."
    if body.casefold() in NEW_WORDS:
        def _purge_existing():
            with session_scope() as session:
                case = session.scalar(
                    select(GrievanceCase)
                    .where(
                        GrievanceCase.user_contact_hash == contact_hash,
                        GrievanceCase.status.notin_([CaseStatus.PURGED.value, CaseStatus.FILED_COMPLETE.value]),
                    )
                    .order_by(GrievanceCase.created_at.desc())
                )
                if case:
                    purge_case_data(session, case, "whatsapp_new")
        await run_in_threadpool(_purge_existing)
        await run_in_threadpool(_mark_message_processed, message_sid)
        return "A new case is ready. Send your issue as a message or voice note with a payment screenshot."

    def _find_active():
        return _active_case(contact_hash)
    active = await run_in_threadpool(_find_active)
    if active and body.casefold() in APPROVE_WORDS and active.status == CaseStatus.READY_FOR_USER_REVIEW.value:
        result = await _approve_case(active.case_reference)
        await run_in_threadpool(_mark_message_processed, message_sid, active.case_reference)
        pdf_url = urljoin(base_url or "", result.get("pdf_url", ""))
        return (
            f"{result.get('message', 'Your PDF is ready.')} "
            f"Download: {pdf_url} "
            "NCH: call 1915, WhatsApp 8800001915, or visit consumerhelpline.gov.in."
        )

    text = body
    audio = None
    audio_name = "whatsapp.ogg"
    screenshots = []
    for data, content_type, filename in media_entries:
        is_image = content_type.startswith("image/") or data.startswith((b"\xff\xd8\xff", b"\x89PNG")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        is_audio = content_type.startswith("audio/") or data.startswith((b"OggS", b"\x1aE\xdf\xa3", b"ID3"))
        if is_image:
            extension = "png" if content_type == "image/png" or data.startswith(b"\x89PNG") else "jpg"
            screenshots.append((data, f"screenshot.{extension}"))
        elif is_audio:
            audio = data
            extension = "ogg" if "ogg" in content_type or data.startswith(b"OggS") else "webm" if "webm" in content_type or data.startswith(b"\x1aE\xdf\xa3") else "mp3"
            audio_name = f"voice-note.{extension}"
        else:
            await run_in_threadpool(_mark_message_processed, message_sid, active.case_reference if active else "NO_CASE")
            return "I could not identify that attachment. Please send a payment screenshot or an audio voice note."
    if len(screenshots) > 3:
        await run_in_threadpool(_mark_message_processed, message_sid, active.case_reference if active else "NO_CASE")
        return "Please send no more than 3 payment screenshots in one message."
    try:
        combined, language, ocr_rows, sanitized = await _process_evidence(
            text=text,
            audio_bytes=audio,
            audio_filename=audio_name,
            screenshots=screenshots,
            preferred_language=None,
        )
    except HTTPException as exc:
        await run_in_threadpool(_mark_message_processed, message_sid, active.case_reference if active else "NO_CASE")
        return str(exc.detail)
    existing = None
    if active:
        try:
            existing = await run_in_threadpool(_load_case_inputs, active.case_reference)
        except HTTPException:
            existing = None
    facts, conflicts = await _extract_and_merge(
        combined,
        ocr_rows,
        existing_facts=existing["facts"] if existing else None,
        language=language,
        user_response=bool(existing),
    )
    if existing:
        result = await run_in_threadpool(
            _save_response,
            active.case_reference,
            combined,
            ocr_rows,
            facts,
            conflicts,
            language,
            sanitized,
            None,
            "whatsapp",
        )
    else:
        result = await run_in_threadpool(
            _persist_new_case,
            sender,
            contact_hash,
            combined,
            ocr_rows,
            facts,
            conflicts,
            language,
            sanitized,
            None,
        )
    await run_in_threadpool(_mark_message_processed, message_sid, result["case_reference"])
    return result.get("follow_up_question_native") or result.get("draft_summary_native") or "Your information has been received."


async def _background_process(
    form: dict,
    media_urls: list[tuple[str, str]],
    message_sid: str | None,
    base_url: str,
):
    try:
        media = []
        for url, content_type in media_urls:
            data, actual_type = await download_twilio_media(url)
            media.append((data, actual_type or content_type, url.rsplit("/", 1)[-1]))
        answer = await _process_message(form, media, message_sid, base_url)
        await send_twilio_reply(str(form.get("From", "")), answer)
    except Exception:
        message_sid = str(form.get("MessageSid", "")) or None
        await run_in_threadpool(_release_message, message_sid)
        logger.exception("whatsapp_background_processing_failed", extra={"event": "whatsapp_error"})
        try:
            await send_twilio_reply(str(form.get("From", "")), "We could not process that message. Please retry or contact the operator.")
        except Exception:
            logger.exception("whatsapp_error_reply_failed")


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    values = {key: str(value) for key, value in form.multi_items() if not hasattr(value, "read")}
    settings = get_settings()
    signature = request.headers.get("X-Twilio-Signature")
    verified = verify_twilio_signature(str(request.url), form, signature)
    if settings.enforce_twilio_signature and not verified:
        raise HTTPException(status_code=403, detail="Invalid Twilio webhook signature.")
    if verified and not settings.enforce_twilio_signature:
        logger.info("twilio_signature_verified", extra={"event": "whatsapp_signature"})
    sid = values.get("MessageSid")
    if sid:
        try:
            state, _ = await run_in_threadpool(reserve_idempotency, SessionLocal, sid, "whatsapp")
        except Exception as exc:
            logger.exception("whatsapp_idempotency_failed")
            raise HTTPException(status_code=409, detail="Could not reserve this webhook message.") from exc
        if state == "replay":
            return _twiml("This message was already processed.")
        if state == "busy":
            return _twiml()

    media_urls = []
    try:
        media_count = int(values.get("NumMedia", "0") or 0)
    except ValueError:
        if sid:
            await run_in_threadpool(_release_message, sid)
        raise HTTPException(status_code=422, detail="Invalid WhatsApp media count.")
    if media_count > 3:
        if sid:
            await run_in_threadpool(_release_message, sid)
        raise HTTPException(status_code=413, detail="At most 3 screenshots are accepted.")
    for index in range(media_count):
        url = values.get(f"MediaUrl{index}", "")
        content_type = values.get(f"MediaContentType{index}", "")
        if url:
            media_urls.append((url, content_type))
    # Signed provider webhooks get an immediate TwiML acknowledgement, with media
    # retrieval and AI processing performed outside the request lifecycle.
    if verified and settings.twilio_account_sid and settings.twilio_auth_token:
        background_tasks.add_task(_background_process, values, media_urls, sid, str(request.base_url))
        return _twiml()

    try:
        media = []
        for url, content_type in media_urls:
            data, actual_type = await download_twilio_media(url)
            media.append((data, actual_type or content_type, url.rsplit("/", 1)[-1]))
        answer = await _process_message(values, media, sid, str(request.base_url))
        return _twiml(answer)
    except HTTPException:
        await run_in_threadpool(_release_message, sid)
        raise
    except Exception as exc:
        await run_in_threadpool(_release_message, sid)
        logger.exception("whatsapp_processing_failed")
        raise HTTPException(status_code=502, detail="WhatsApp message processing failed.") from exc
