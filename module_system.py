"""Система модулей Ады: подключаемые возможности лежат в папке modules/.

Модуль — это папка `modules/<имя>/` с файлом `__init__.py` либо одиночный файл
`modules/<имя>.py`. Внутри должна быть функция `setup(ctx)`, которая регистрирует
инструменты, хуки и описание возможностей.

Включение и отключение через .env по имени папки модуля:

    MODULE_VISION=true
    MODULE_VOICE=false

Порядок поиска флага: `MODULE_<ИМЯ>`, затем устаревшее `<ИМЯ>_ENABLED`, затем
просто `<ИМЯ>`. Если переменной нет, берётся `DEFAULT_ENABLED` самого модуля.

Отключённый модуль вообще не импортируется, поэтому его зависимости (torch,
whisper, PIL и прочее) не требуются, пока он выключен.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import config

logger = logging.getLogger("Ada.Modules")

# Точки расширения. Модуль объявляет обработчик через ctx.register_hook(...).
HOOK_STARTUP = "startup"                  # async () -> None; после подключения к Telegram
HOOK_SHUTDOWN = "shutdown"                # async () -> None; при остановке Ады
HOOK_MEDIA_LABEL = "media_label"          # async (telethon message) -> str; пометка вида «[фото: ...]»
HOOK_TRANSCRIBE = "transcribe_audio"      # async (bytes, suffix) -> str; речь в текст
HOOK_AUDIO_RECORDER = "audio_recorder"    # () -> recorder с методами start/stop/cancel и полем active
HOOK_MESSAGE_CONTEXT = "message_context"  # async (telethon message) -> str; доп. контекст для промпта
HOOK_AUTONOMY_TICK = "autonomy_tick"      # async (autonomy: AutonomyEngine) -> None; каждый цикл автономности
HOOK_LOCAL_MENU = "local_menu"            # (on_submit, transcribe) -> None; запускает локальное меню на ПК


@dataclass
class ModuleInfo:
    """Состояние одного модуля для диагностики и команды «модули»."""

    name: str
    enabled: bool
    description: str = ""
    version: str = ""
    loaded: bool = False
    error: str = ""
    tools: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)

    def status_line(self) -> str:
        if not self.enabled:
            return f"[выкл] {self.name} — отключён в .env (MODULE_{_env_name(self.name)}=false)"
        if self.error:
            return f"[ошибка] {self.name} — {self.error}"
        details = []
        if self.tools:
            details.append("инструменты: " + ", ".join(self.tools))
        if self.hooks:
            details.append("хуки: " + ", ".join(self.hooks))
        suffix = ("; " + "; ".join(details)) if details else ""
        return f"[вкл] {self.name} — работает{suffix}"


def _env_name(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.upper())


class ModuleContext:
    """То, что модуль получает от Ады, и то, через что он отдаёт свои возможности."""

    def __init__(self, registry: "ModuleRegistry", module_name: str, services: dict[str, Any]):
        self._registry = registry
        self._module_name = module_name
        self._services = services
        self.logger = logging.getLogger(f"Ada.Module.{module_name}")
        self.config = config

    @property
    def name(self) -> str:
        return self._module_name

    @property
    def db(self):
        return self._services.get("db")

    @property
    def client(self):
        """Telegram-клиент. На момент setup() он создан, но ещё не подключён."""
        return self._services.get("client")

    @property
    def tools(self):
        """AgentTools: проверки прав (is_admin, pc_allowed) и действия в Telegram."""
        return self._services.get("tools")

    @property
    def emotions(self):
        return self._services.get("emotions")

    def service(self, key: str, default=None):
        return self._services.get(key, default)

    def register_tool(
        self,
        name: str,
        func: Callable,
        description: str,
        properties: dict | None = None,
        required: Iterable[str] | None = None,
    ) -> None:
        """Добавляет инструмент, который сможет вызывать модель."""
        self._registry._add_tool(self._module_name, name, func, description, properties or {}, list(required or []))

    def register_hook(self, hook: str, func: Callable) -> None:
        """Подписывает модуль на точку расширения (константы HOOK_*)."""
        self._registry._add_hook(self._module_name, hook, func)

    def register_capability(self, text: str) -> None:
        """Строка для list_my_capabilities: что Ада умеет благодаря модулю."""
        self._registry._add_capability(self._module_name, text)


class ModuleRegistry:
    """Хранит загруженные модули и раздаёт их инструменты и хуки остальному коду."""

    def __init__(self):
        self.modules: dict[str, ModuleInfo] = {}
        self._hooks: dict[str, list[tuple[str, Callable]]] = defaultdict(list)
        self._tools: dict[str, Callable] = {}
        self._specs: list[dict] = []
        self._capabilities: list[str] = []

    # --- регистрация (вызывается из ModuleContext) ---

    def _add_tool(self, module: str, name: str, func: Callable, description: str,
                  properties: dict, required: list[str]) -> None:
        if name in self._tools:
            logger.warning("Модуль %s: инструмент %s уже зарегистрирован, пропускаю", module, name)
            return
        self._tools[name] = func
        self._specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        })
        self.modules[module].tools.append(name)

    def _add_hook(self, module: str, hook: str, func: Callable) -> None:
        self._hooks[hook].append((module, func))
        if hook not in self.modules[module].hooks:
            self.modules[module].hooks.append(hook)

    def _add_capability(self, module: str, text: str) -> None:
        self._capabilities.append(text)

    # --- чтение ---

    def tool_functions(self) -> dict[str, Callable]:
        return dict(self._tools)

    def tool_specs(self) -> list[dict]:
        return [dict(spec) for spec in self._specs]

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def active_names(self) -> list[str]:
        return [info.name for info in self.modules.values() if info.loaded]

    def has_hook(self, hook: str) -> bool:
        return bool(self._hooks.get(hook))

    def status_report(self) -> str:
        if not self.modules:
            return "Модулей нет: папка modules пуста."
        return "\n".join(info.status_line() for info in self.modules.values())

    # --- вызов хуков ---

    async def call_first(self, hook: str, *args, **kwargs):
        """Результат первого обработчика, который вернул непустое значение."""
        for module, func in self._hooks.get(hook, []):
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    return result
            except Exception:
                logger.exception("Модуль %s: ошибка в хуке %s", module, hook)
        return None

    async def call_all(self, hook: str, *args, **kwargs) -> list:
        """Все обработчики по очереди; ошибка одного не мешает остальным."""
        results = []
        for module, func in self._hooks.get(hook, []):
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    results.append(result)
            except Exception:
                logger.exception("Модуль %s: ошибка в хуке %s", module, hook)
        return results

    def call_first_sync(self, hook: str, *args, **kwargs):
        """Для синхронных хуков вроде audio_recorder."""
        for module, func in self._hooks.get(hook, []):
            try:
                result = func(*args, **kwargs)
                if result:
                    return result
            except Exception:
                logger.exception("Модуль %s: ошибка в хуке %s", module, hook)
        return None


_registry = ModuleRegistry()


def registry() -> ModuleRegistry:
    """Текущий реестр. До load_modules() он пустой, поэтому код работает и без модулей."""
    return _registry


def is_enabled(name: str, default: bool = True) -> bool:
    """Флаг модуля из .env: MODULE_<ИМЯ>, затем <ИМЯ>_ENABLED, затем <ИМЯ>."""
    import os

    env = _env_name(name)
    for key in (f"MODULE_{env}", f"{env}_ENABLED", env):
        raw = os.getenv(key)
        if raw is not None and raw.strip():
            return raw.strip().lower() in {"true", "1", "yes", "on", "да"}
    return default


def discover() -> list[str]:
    """Имена модулей в папке modules: подпапки с __init__.py и одиночные .py файлы."""
    modules_dir = config.MODULES_DIR
    if not modules_dir.is_dir():
        return []
    names = set()
    for entry in modules_dir.iterdir():
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            names.add(entry.stem)
    return sorted(names)


def load_modules(**services) -> ModuleRegistry:
    """Импортирует включённые модули и вызывает их setup(ctx).

    services — то, что модули получают через ModuleContext: db, client, tools, emotions.
    Падение одного модуля не мешает запуску Ады: ошибка попадает в лог и в «модули».
    """
    global _registry
    _registry = ModuleRegistry()

    for name in discover():
        default_enabled = True
        enabled = is_enabled(name, default_enabled)
        info = ModuleInfo(name=name, enabled=enabled)
        _registry.modules[name] = info
        if not enabled:
            logger.info("Модуль %s выключен в .env", name)
            continue
        try:
            module = importlib.import_module(f"modules.{name}")
            info.description = getattr(module, "DESCRIPTION", "")
            info.version = getattr(module, "VERSION", "")
            # DEFAULT_ENABLED учитывается только когда в .env флага нет.
            if not is_enabled(name, bool(getattr(module, "DEFAULT_ENABLED", True))):
                info.enabled = False
                logger.info("Модуль %s выключен по умолчанию (DEFAULT_ENABLED=False)", name)
                continue
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise AttributeError("в модуле нет функции setup(ctx)")
            setup(ModuleContext(_registry, name, services))
            info.loaded = True
            logger.info("Модуль %s подключён%s", name, f": {info.description}" if info.description else "")
        except Exception as exc:
            info.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Не удалось подключить модуль %s", name)

    active = _registry.active_names()
    logger.info("Активные модули: %s", ", ".join(active) if active else "нет")
    return _registry


# --- Удобные обёртки для остального кода ---

async def media_label(message) -> str:
    """Текстовая пометка о медиа в сообщении; пустая строка, если модуля зрения нет."""
    return await _registry.call_first(HOOK_MEDIA_LABEL, message) or ""


async def message_context(message) -> str:
    """Дополнительный контекст от модулей для системного промпта."""
    parts = await _registry.call_all(HOOK_MESSAGE_CONTEXT, message)
    return "".join(part if part.endswith("\n") else part + "\n" for part in parts if part)


async def transcribe_audio(audio: bytes, suffix: str = ".wav") -> str | None:
    """Речь в текст через модуль голоса; None, если модуль выключен."""
    return await _registry.call_first(HOOK_TRANSCRIBE, audio, suffix)


def audio_recorder():
    """Диктофон от модуля голоса; None, если модуль выключен."""
    return _registry.call_first_sync(HOOK_AUDIO_RECORDER)


def start_local_menu(on_submit, transcribe) -> bool:
    """Запускает локальное меню ПК из модуля pc_control; False, если модуль выключен."""
    return bool(_registry.call_first_sync(HOOK_LOCAL_MENU, on_submit, transcribe))


def voice_available() -> bool:
    return _registry.has_hook(HOOK_TRANSCRIBE)


def vision_available() -> bool:
    return _registry.has_hook(HOOK_MEDIA_LABEL)


async def run_startup_hooks() -> None:
    await _registry.call_all(HOOK_STARTUP)


async def run_shutdown_hooks() -> None:
    await _registry.call_all(HOOK_SHUTDOWN)


async def run_autonomy_tick(autonomy) -> None:
    """Периодические задачи модулей (напоминания о расписании, почте и т.п.)."""
    await _registry.call_all(HOOK_AUTONOMY_TICK, autonomy)
