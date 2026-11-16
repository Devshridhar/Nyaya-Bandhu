"""Speech transcription with OpenAI primary and Sarvam fallback when configured."""
from __future__ import annotations

import asyncio
import base64
import io
import logging

import httpx

from app.core.config import get_settings
from app.services.ai_client import external_retry, get_openai_client, speech_model
from app.services.evidence_service import validate_audio

logger = logging.getLogger("nyaya.speech")
INDIAN_TERMS = "Indian consumer complaint terms: UTR, refund, failed, Swiggy, Flipkart, rupees, deducted, Nodal Officer, UPI."


@external_retry
async def _openai_transcribe(data: bytes, mime_type: str, filename: str, language: str | None) -> str:
    audio_file = io.BytesIO(data)
    audio_file.name = filename or "voice-note.webm"
    options = {
        "file": (audio_file.name, audio_file, mime_type),
        "model": speech_model(),
        "prompt": INDIAN_TERMS,
        "response_format": "json",
    }
    if language and len(language) >= 2:
        options["language"] = language[:2]
    response = await get_openai_client().audio.transcriptions.create(**options)
    return response.text.strip()


@external_retry
async def _sarvam_transcribe(data: bytes, filename: str, language: str | None) -> str:
    api_key = get_settings().sarvam_api_key
    if not api_key:
        raise RuntimeError("Sarvam fallback is not configured.")
    files = {"file": (filename or "voice-note.webm", data, "application/octet-stream")}
    payload = {"model": "saaras:v2", "language_code": language or "unknown", "mode": "transcribe"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": api_key},
            data=payload,
            files=files,
        )
        response.raise_for_status()
        result = response.json()
    text = result.get("transcript") or result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Sarvam returned no transcript.")
    return text.strip()


def _bhashini_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.bhashini_user_id
        and settings.bhashini_ulca_api_key
        and settings.bhashini_inference_key
        and settings.bhashini_service_id
    )


@external_retry
async def _bhashini_transcribe(data: bytes, filename: str, language: str | None) -> str:
    settings = get_settings()
    # Dhruva ASR accepts a configured Bhashini service ID and 16 kHz WAV. Convert
    # in an async subprocess so compressed WhatsApp formats are handled safely.
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0",
        "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    wav, stderr = await process.communicate(data)
    if process.returncode != 0 or not wav.startswith(b"RIFF"):
        raise RuntimeError("Audio could not be converted for Bhashini ASR.")
    source_language = (language or "hi").split("-", 1)[0]
    payload = {
        "pipelineTasks": [{
            "taskType": "asr",
            "config": {
                "language": {"sourceLanguage": source_language},
                "serviceId": settings.bhashini_service_id,
                "audioFormat": "wav",
                "samplingRate": 16000,
            },
        }],
        "inputData": {"audio": [{"audioContent": base64.b64encode(wav).decode("ascii")}]},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://dhruva-api.bhashini.gov.in/services/inference/pipeline",
            headers={
                "Authorization": settings.bhashini_inference_key,
                "userID": settings.bhashini_user_id,
                "ulcaApiKey": settings.bhashini_ulca_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
    try:
        transcription = result["pipelineResponse"][0]["output"][0]["source"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Bhashini returned no transcription.") from exc
    if not isinstance(transcription, str) or not transcription.strip():
        raise RuntimeError("Bhashini returned an empty transcription.")
    return transcription.strip()


async def transcribe_audio(data: bytes, filename: str, language: str | None = None) -> str:
    mime_type = validate_audio(data, filename)
    try:
        return await _openai_transcribe(data, mime_type, filename, language)
    except Exception:
        logger.warning("primary_transcription_failed_trying_fallback", extra={"event": "speech_fallback"})
        if _bhashini_configured():
            try:
                return await _bhashini_transcribe(data, filename, language)
            except Exception:
                logger.warning("bhashini_transcription_failed_trying_secondary_fallback", extra={"event": "bhashini_fallback"})
        if get_settings().sarvam_api_key:
            return await _sarvam_transcribe(data, filename, language)
        raise
