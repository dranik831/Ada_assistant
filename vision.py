"""Зрение: локальная VLM (например Qwen3-VL через Ollama/LM Studio) превращает картинку в текст."""
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
        _client = AsyncOpenAI(base_url=config.VISION_BASE_URL, api_key=config.VISION_API_KEY or "local",
                              timeout=config.VISION_TIMEOUT_SECONDS)
    url = "data:image/jpeg;base64," + base64.b64encode(_to_jpeg(raw)).decode()
    resp = await _client.chat.completions.create(
        model=config.VISION_MODEL, max_tokens=600,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": prompt},
        ]}],
    )
    return (resp.choices[0].message.content or "").strip()


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
