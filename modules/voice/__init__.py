"""Модуль голоса: запись с микрофона и распознавание речи.

Включение: MODULE_VOICE=true в .env.

Используется локальным меню на ПК (Ctrl+Alt+A, запись по Ctrl+Alt+Space).
Распознавание — Whisper medium на этом компьютере или внешний
OpenAI-совместимый сервер /v1/audio/transcriptions.

Настройки .env:
  WHISPER_MODE=local   # local — модель на этом ПК; api — внешний сервер
  WHISPER_API_URL=
  WHISPER_API_KEY=

Даёт:
  хук audio_recorder   — диктофон для локального меню;
  хук transcribe_audio — речь в текст;
  хук startup          — фоновая загрузка модели, чтобы первая запись не ждала.
"""

import logging

from module_system import HOOK_AUDIO_RECORDER, HOOK_STARTUP, HOOK_TRANSCRIBE, ModuleContext

from .recorder import Recorder
from .transcribe import preload, transcribe

DESCRIPTION = "голос: запись с микрофона и распознавание речи"
VERSION = "1.0"
DEFAULT_ENABLED = True

logger = logging.getLogger("Ada.Module.voice")

__all__ = ["Recorder", "preload", "transcribe", "setup"]


def setup(ctx: ModuleContext) -> None:
    async def on_startup() -> None:
        preload()

    ctx.register_hook(HOOK_STARTUP, on_startup)
    ctx.register_hook(HOOK_TRANSCRIBE, transcribe)
    ctx.register_hook(HOOK_AUDIO_RECORDER, Recorder)
    ctx.register_capability(
        "Голосовой ввод в локальном меню на ПК Создателя: Ctrl+Alt+Space записывает речь и переводит её в текст."
    )
