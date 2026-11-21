"""Модуль зрения: Ада видит фото, стикеры и экран своего ПК.

Включение: MODULE_VISION=true в .env.

Картинка уходит в OpenAI-совместимую vision-модель (например Qwen3-VL через
Ollama или LM Studio) и возвращается текстом, который попадает в контекст
разговора.

Настройки .env:
  VISION_BASE_URL=http://localhost:11434/v1
  VISION_MODEL=qwen3-vl:2b
  VISION_API_KEY=
  VISION_TIMEOUT_SECONDS=120

Даёт:
  хук media_label  — пометка «[фото: ...]» к сообщениям с картинками;
  инструмент look_at_screen — скриншот рабочего стола Создателя.
"""

import asyncio
import logging

from module_system import HOOK_MEDIA_LABEL, ModuleContext

from .engine import describe, grab_screen, media_label

DESCRIPTION = "зрение: фото, стикеры и экран ПК"
VERSION = "1.0"
DEFAULT_ENABLED = False

logger = logging.getLogger("Ada.Module.vision")

__all__ = ["describe", "grab_screen", "media_label", "setup"]


def setup(ctx: ModuleContext) -> None:
    tools = ctx.tools

    async def look_at_screen(question: str = "") -> str:
        """Скриншот рабочего стола Создателя и его описание vision-моделью."""
        if tools is None or not tools._pc_allowed():
            return "Смотреть на экран может только Создатель в личном чате."
        try:
            shot = await asyncio.to_thread(grab_screen)
            prompt = (question or "Что сейчас на экране?") + (
                " Отвечай по-русски. Если на экране есть важный текст, процитируй его."
            )
            return await describe(shot, prompt) or "Модель ничего не ответила."
        except Exception as exc:
            logger.exception("Ошибка look_at_screen")
            return f"Не удалось посмотреть на экран: {type(exc).__name__}"

    ctx.register_hook(HOOK_MEDIA_LABEL, media_label)
    ctx.register_tool(
        "look_at_screen",
        look_at_screen,
        "Сделать скриншот рабочего стола Создателя и посмотреть на него. question — что именно "
        "нужно понять или найти на экране. Вызывай, когда Создатель просит посмотреть на экран "
        "или подсказать по тому, что на нём. Только Создатель.",
        {"question": {"type": "string"}},
    )
    ctx.register_capability(
        "look_at_screen: посмотреть на экран ПК Создателя; фото и стикеры в чатах описываются автоматически."
    )
