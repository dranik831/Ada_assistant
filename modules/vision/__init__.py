"""Модуль зрения: Ада видит фото, стикеры и экран своего ПК.

Включение: MODULE_VISION=true в .env.

По умолчанию картинка уходит в основной LLM-провайдер (тот же, что отвечает
в чатах, через ACTIVE_LLM_BASE_URL/ACTIVE_LLM_MODEL) — отдельный vision-сервер
не нужен, если провайдер поддерживает image_url в сообщениях (как большинство
современных OpenAI-совместимых агрегаторов). Текст возвращается в контекст
разговора.

Чтобы использовать отдельную vision-модель (например локальную VLM через
Ollama/LM Studio), заполните VISION_BASE_URL в .env — тогда модуль переключится
на неё вместо основного провайдера:
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
DEFAULT_ENABLED = True

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
