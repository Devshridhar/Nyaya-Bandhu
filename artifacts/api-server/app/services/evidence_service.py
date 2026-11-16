"""Upload validation, magic-byte checks, image decode and metadata removal."""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import get_settings

AUDIO_SIGNATURES = {
    "audio/wav": (b"RIFF",),
    "audio/mpeg": (b"ID3",),
    "audio/ogg": (b"OggS",),
    "audio/webm": (b"\x1aE\xdf\xa3",),
    "audio/mp4": (b"\x00\x00\x00",),
}
IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass(frozen=True)
class SanitizedImage:
    data: bytes
    content_type: str
    width: int
    height: int


def validate_audio(data: bytes, filename: str = "") -> str:
    limit = get_settings().max_upload_bytes
    if not data or len(data) > limit:
        raise ValueError("Audio must be non-empty and no larger than 10 MB.")
    lower = filename.lower()
    allowed_extensions = (".wav", ".mp3", ".ogg", ".m4a", ".webm")
    if lower and not lower.endswith(allowed_extensions):
        raise ValueError("Audio must be WAV, MP3, OGG, M4A, or WebM.")
    detected = None
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        detected = "audio/wav"
    elif data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        detected = "audio/mpeg"
    elif data.startswith(b"OggS"):
        detected = "audio/ogg"
    elif data.startswith(b"\x1aE\xdf\xa3"):
        detected = "audio/webm"
    elif len(data) >= 12 and data[4:8] == b"ftyp":
        detected = "audio/mp4"
    if detected is None:
        raise ValueError("Audio file contents do not match a supported audio format.")
    extension_types = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
    }
    extension = next((item for item in allowed_extensions if lower.endswith(item)), "")
    if extension and extension_types[extension] != detected:
        raise ValueError("Audio file extension does not match its contents.")
    return detected


def sanitize_image(data: bytes, filename: str = "") -> SanitizedImage:
    limit = get_settings().max_upload_bytes
    if not data or len(data) > limit:
        raise ValueError("Each screenshot must be non-empty and no larger than 10 MB.")
    lower = filename.lower()
    if lower and not lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise ValueError("Screenshots must be JPEG, PNG, or WebP.")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in IMAGE_FORMATS:
                raise ValueError("Screenshot contents do not match JPEG, PNG, or WebP.")
            original.verify()
        with Image.open(io.BytesIO(data)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True, exif=b"")
            return SanitizedImage(output.getvalue(), "image/jpeg", image.width, image.height)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Could not read screenshot. Upload a valid JPEG, PNG, or WebP image.") from exc
