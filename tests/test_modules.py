"""Самопроверка системы модулей: python tests/test_modules.py

Проверяет обнаружение модулей, флаги .env, регистрацию инструментов и хуков,
изоляцию падающего модуля и то, что ядро работает с пустым реестром.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import module_system  # noqa: E402


def test_discover():
    found = module_system.discover()
    assert "vision" in found, found
    assert "voice" in found, found


def test_env_flags():
    os.environ["MODULE_DEMO"] = "false"
    assert not module_system.is_enabled("demo", True)
    os.environ["MODULE_DEMO"] = "true"
    assert module_system.is_enabled("demo", False)
    del os.environ["MODULE_DEMO"]
    # Устаревший вид флага продолжает работать.
    os.environ["DEMO_ENABLED"] = "yes"
    assert module_system.is_enabled("demo", False)
    del os.environ["DEMO_ENABLED"]
    assert module_system.is_enabled("demo", True)
    assert not module_system.is_enabled("demo", False)


def test_empty_registry_is_safe():
    registry = module_system.ModuleRegistry()
    assert registry.tool_functions() == {}
    assert registry.tool_specs() == []
    assert registry.capabilities() == []
    assert not registry.has_hook(module_system.HOOK_MEDIA_LABEL)
    assert asyncio.run(registry.call_first(module_system.HOOK_MEDIA_LABEL, None)) is None
    assert asyncio.run(registry.call_all(module_system.HOOK_STARTUP)) == []


def test_registration_and_isolation():
    registry = module_system.ModuleRegistry()
    registry.modules["ok"] = module_system.ModuleInfo(name="ok", enabled=True)
    registry.modules["bad"] = module_system.ModuleInfo(name="bad", enabled=True)
    good = module_system.ModuleContext(registry, "ok", {})
    bad = module_system.ModuleContext(registry, "bad", {})

    async def do_thing(value: str = "") -> str:
        return f"сделано:{value}"

    async def boom(_message):
        raise RuntimeError("модуль сломан")

    async def label(_message):
        return "[фото: кот]"

    good.register_tool("do_thing", do_thing, "Тестовый инструмент.", {"value": {"type": "string"}}, ["value"])
    good.register_capability("do_thing: тестовая возможность")
    # Сломанный хук стоит первым: ядро должно пережить его и дойти до рабочего.
    bad.register_hook(module_system.HOOK_MEDIA_LABEL, boom)
    good.register_hook(module_system.HOOK_MEDIA_LABEL, label)

    assert "do_thing" in registry.tool_functions()
    assert registry.capabilities() == ["do_thing: тестовая возможность"]
    spec = registry.tool_specs()[0]["function"]
    assert spec["name"] == "do_thing"
    assert spec["parameters"]["required"] == ["value"]
    assert asyncio.run(registry.call_first(module_system.HOOK_MEDIA_LABEL, None)) == "[фото: кот]"
    assert asyncio.run(registry.tool_functions()["do_thing"](value="x")) == "сделано:x"

    # Повторная регистрация того же имени не затирает рабочий инструмент.
    async def other() -> str:
        return "другое"

    bad.register_tool("do_thing", other, "Дубликат.", {}, [])
    assert registry.tool_functions()["do_thing"] is do_thing


def test_load_modules_reports_disabled():
    os.environ["MODULE_VISION"] = "false"
    os.environ["MODULE_VOICE"] = "false"
    try:
        registry = module_system.load_modules(db=None, client=None, tools=None, emotions=None)
        assert registry.active_names() == []
        assert registry.tool_functions() == {}
        report = registry.status_report()
        assert "vision" in report and "voice" in report
        assert "[выкл]" in report
        # С выключенными модулями обёртки ядра остаются безопасными.
        assert asyncio.run(module_system.media_label(None)) == ""
        assert asyncio.run(module_system.transcribe_audio(b"")) is None
        assert module_system.audio_recorder() is None
        assert not module_system.voice_available()
        assert not module_system.vision_available()
        asyncio.run(module_system.run_startup_hooks())
        asyncio.run(module_system.run_shutdown_hooks())
    finally:
        os.environ.pop("MODULE_VISION", None)
        os.environ.pop("MODULE_VOICE", None)


if __name__ == "__main__":
    test_discover()
    test_env_flags()
    test_empty_registry_is_safe()
    test_registration_and_isolation()
    test_load_modules_reports_disabled()
    print("OK: система модулей работает, выключенные модули не ломают ядро")
