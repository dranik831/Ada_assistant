"""Шаблон модуля Ады. Скопируйте в modules/<имя>.py или modules/<имя>/__init__.py.

Имена, начинающиеся с подчёркивания, Ада не загружает, поэтому сам шаблон
никогда не подключается.

Чтобы получился рабочий модуль:
  1. Скопируйте файл под своим именем, например modules/weather.py
  2. Опишите DESCRIPTION и оставьте нужные части setup()
  3. Добавьте MODULE_WEATHER=true в .env и .env.example
  4. Перезапустите Аду и проверьте командой «модули» в режиме редактирования
"""

import logging

from module_system import (
    HOOK_MESSAGE_CONTEXT,
    HOOK_SHUTDOWN,
    HOOK_STARTUP,
    ModuleContext,
)

DESCRIPTION = "короткое описание модуля"
VERSION = "1.0"
DEFAULT_ENABLED = False  # значение флага, когда в .env его нет

logger = logging.getLogger("Ada.Module.template")


def setup(ctx: ModuleContext) -> None:
    """Вызывается один раз при запуске Ады, если модуль включён."""

    # --- Инструмент, который сможет вызвать модель ---
    async def example_action(text: str, count: int = 1) -> str:
        # Права владельца проверяйте через ctx.tools:
        #   ctx.tools._is_admin()   — действие только для Создателя
        #   ctx.tools._pc_allowed() — всё, что касается его компьютера
        if ctx.tools is not None and not ctx.tools._is_admin():
            return "Это действие доступно только Создателю."
        ctx.logger.info("example_action: %s x%s", text, count)
        return " ".join([text] * max(1, min(int(count), 10)))

    ctx.register_tool(
        "example_action",
        example_action,
        "Пример инструмента: повторяет text count раз. Замените на своё описание.",
        {"text": {"type": "string"}, "count": {"type": "integer"}},
        ["text"],
    )

    # --- Строка для list_my_capabilities ---
    ctx.register_capability("example_action: пример возможности из модуля-шаблона")

    # --- Хуки: подключайте только нужные ---
    async def on_startup() -> None:
        """После подключения к Telegram: прогрев кэша, фоновые задачи."""
        ctx.logger.info("Модуль %s запущен", ctx.name)

    async def on_shutdown() -> None:
        """При остановке Ады: закрыть соединения, сохранить состояние."""
        ctx.logger.info("Модуль %s остановлен", ctx.name)

    async def on_message_context(message) -> str:
        """Дополнительная строка контекста к сообщению; пустая строка — ничего не добавлять."""
        return ""

    ctx.register_hook(HOOK_STARTUP, on_startup)
    ctx.register_hook(HOOK_SHUTDOWN, on_shutdown)
    ctx.register_hook(HOOK_MESSAGE_CONTEXT, on_message_context)
