"""Распознавание речи: Whisper medium локально или внешний сервер по API.

.env:
  WHISPER_MODE=local   # local — модель на этом ПК; api — OpenAI-совместимый /v1/audio/transcriptions
  WHISPER_API_URL=     # для api, например http://my-server:9000/v1/audio/transcriptions
  WHISPER_API_KEY=     # если сервер требует Bearer-токен
"""
import asyncio
import logging
import tempfile
import threading
from pathlib import Path

import aiohttp

import config

logger = logging.getLogger("Ada.Whisper")

_model = None
_model_lock = threading.Lock()


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            import torch
            import whisper
            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("Загружаю Whisper medium (%s)…", device)
            _model = whisper.load_model("medium", device=device)
            logger.info("Whisper medium готова")
    return _model


def preload():
    """Фоновая загрузка модели при старте, чтобы первое распознавание не ждало скачивания."""
    if config.WHISPER_MODE == "local":
        threading.Thread(target=_safe_load, daemon=True, name="whisper-load").start()


def _safe_load():
    try:
        _load_model()
    except Exception as e:
        logger.error("Whisper не загрузилась: %s", e)


def _transcribe_local(audio: bytes, suffix: str) -> str:
    model = _load_model()
    # whisper читает файл через ffmpeg, поэтому подходит любой формат (wav, ogg из Telegram и т.д.)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio)
    try:
        result = model.transcribe(tmp.name, language="ru", fp16=model.device.type == "cuda")
        return result["text"].strip()
    finally:
        Path(tmp.name).unlink(missing_ok=True)


async def _transcribe_api(audio: bytes, suffix: str) -> str:
    if not config.WHISPER_API_URL:
        raise RuntimeError("WHISPER_MODE=api, но WHISPER_API_URL не задан")
    form = aiohttp.FormData()
    form.add_field("file", audio, filename=f"audio{suffix}")
    form.add_field("model", "whisper-1")
    form.add_field("language", "ru")
    headers = {"Authorization": f"Bearer {config.WHISPER_API_KEY}"} if config.WHISPER_API_KEY else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        async with session.post(config.WHISPER_API_URL, data=form, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Whisper API {resp.status}: {(await resp.text())[:200]}")
            return ((await resp.json()).get("text") or "").strip()


async def transcribe(audio: bytes, suffix: str = ".wav") -> str:
    """Текст из аудио. Локальная модель работает в отдельном потоке и не блокирует бота."""
    if config.WHISPER_MODE == "api":
        return await _transcribe_api(audio, suffix)
    return await asyncio.to_thread(_transcribe_local, audio, suffix)
