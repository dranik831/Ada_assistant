"""Зрение: картинка уходит в основной LLM-провайдер (тот же, что ведёт диалоги).

Если провайдер настроен на VISION_BASE_URL/VISION_MODEL отдельно (например локальная
VLM через Ollama/LM Studio), используются они; иначе — ACTIVE_LLM_* того же провайдера,
что и обычные ответы (OpenAI-совместимый vision через image_url работает у многих
современных моделей, включая большинство OpenAI-совместимых агрегаторов)."""
import base64
import io
import logging

from openai import AsyncOpenAI
from PIL import Image, ImageGrab

import config

logger = logging.getLogger("Ada.Vision")

PHOTO_PROMPT = (
    "Опиши изображение по-русски в 1-3 предложениях. Если на нём есть текст, перепиши его. "
    "Если это стикер, назови эмоцию или смысл."
)
_client: AsyncOpenAI | None = None

# Пустой VISION_BASE_URL (по умолчанию) означает «используй основной провайдер».
_USE_MAIN_PROVIDER = not config.VISION_BASE_URL.strip()
_BASE_URL = config.ACTIVE_LLM_BASE_URL if _USE_MAIN_PROVIDER else config.VISION_BASE_URL
_API_KEY = config.ACTIVE_LLM_API_KEY if _USE_MAIN_PROVIDER else (config.VISION_API_KEY or "local")
_MODEL = config.ACTIVE_LLM_MODEL if _USE_MAIN_PROVIDER else config.VISION_MODEL


def _to_jpeg(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def grab_screen() -> bytes:
    buf = io.BytesIO()
    ImageGrab.grab().save(buf, "PNG")
    return buf.getvalue()


async def describe(raw: bytes, prompt: str) -> str:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url=_BASE_URL, api_key=_API_KEY, timeout=config.VISION_TIMEOUT_SECONDS)
    url = "data:image/jpeg;base64," + base64.b64encode(_to_jpeg(raw)).decode()
    try:
        resp = await _client.chat.completions.create(
            model=_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": prompt},
            ]}],
        )
    except Exception as exc:
        source = "основной провайдер" if _USE_MAIN_PROVIDER else "VISION_BASE_URL"
        raise RuntimeError(f"vision-запрос к {source} ({_MODEL}) не удался: {exc}") from exc
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError(f"vision-модель вернула пустой ответ ({resp.choices[0].finish_reason})")
    return text


async def media_label(message) -> str:
    """Пометка вида «[фото: ...]» для фото и стикеров; пустая строка, если картинки нет."""
    kind = "стикер" if message.sticker else "фото" if message.photo else ""
    if not kind:
        return ""
    if message.sticker and message.file.mime_type != "image/webp":
        return f"[анимированный стикер {message.file.emoji or ''}]".replace(" ]", "]")
    try:
        return f"[{kind}: {await describe(await message.download_media(bytes), PHOTO_PROMPT)}]"
    except Exception:
        logger.exception("Не удалось описать %s", kind)
        return f"[{kind}: не удалось рассмотреть]"
