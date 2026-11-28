"""Regression checks for forwarded photos without a Telegram mention."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import module_system


def test_forwarded_photo_label():
    registry = module_system.ModuleRegistry()
    registry.modules["vision"] = module_system.ModuleInfo(name="vision", enabled=True)
    message = SimpleNamespace(photo=object(), sticker=None, mentioned=False, fwd_from=object())

    async def describe_photo(received):
        assert received is message
        return "[фото: изображение здания]"

    module_system.ModuleContext(registry, "vision").register_hook(module_system.HOOK_MEDIA_LABEL, describe_photo)
    with patch.object(module_system, "_registry", registry):
        assert asyncio.run(module_system.media_label(message)) == "[фото: изображение здания]"
    with patch.object(module_system, "_registry", module_system.ModuleRegistry()):
        assert asyncio.run(module_system.media_label(message)) == "[фото: не удалось рассмотреть]"


if __name__ == "__main__":
    test_forwarded_photo_label()
    print("Group media checks passed")
