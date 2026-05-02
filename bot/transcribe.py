"""
Транскрипция голосовых через Groq Whisper API (бесплатный тариф).

API: https://api.groq.com/openai/v1/audio/transcriptions
Модель: whisper-large-v3 (или whisper-large-v3-turbo для скорости)
Лимит free tier: 2000 запросов/день, 25 МБ на файл.
"""

from __future__ import annotations

import logging
from io import BytesIO

import httpx

from .config import GROQ_API_KEY

log = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"


def transcribe_voice_bytes(audio_bytes: bytes, *, filename: str = "voice.ogg",
                           language: str = "ru") -> str | None:
    """
    Возвращает распознанный текст или None при ошибке.

    Telegram присылает голосовые в формате OGG/Opus. Groq принимает.
    """
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY не задан, транскрипция недоступна")
        return None

    try:
        with httpx.Client(timeout=60) as client:
            files = {
                "file": (filename, BytesIO(audio_bytes), "audio/ogg"),
            }
            data = {
                "model": GROQ_MODEL,
                "language": language,
                "response_format": "json",
                "temperature": "0",
            }
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
            }
            response = client.post(GROQ_API_URL, files=files, data=data, headers=headers)
            response.raise_for_status()
            payload = response.json()
            return (payload.get("text") or "").strip()
    except httpx.HTTPStatusError as e:
        log.error("Groq HTTP %s: %s", e.response.status_code, e.response.text[:300])
    except (httpx.HTTPError, OSError, ValueError) as e:
        log.error("Groq transcribe failed: %s", e)
    return None
