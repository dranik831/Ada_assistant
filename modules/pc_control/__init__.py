"""Модуль управления ПК Создателя: терминал, файлы, буфер обмена, запуск программ,
локальное меню на рабочем столе (Ctrl+Alt+A).

Включение: MODULE_PC_CONTROL=true в .env.

Всё здесь доступно только Создателю и только в личном чате или локальном меню
(проверка AgentTools._pc_allowed — Создатель + chat_id > 0). В группе пакет
сообщений идёт от имени первого автора, поэтому там команда не выполняется.

Даёт инструменты:
  execute_system_command, read_file, write_file, list_directory,
  open_on_pc, open_terminal, clipboard, show_on_desktop
и локальное меню Win/Ctrl+Alt+A с полем ввода и голосовым вводом (если включён
модуль voice).
"""

import asyncio
import logging

from module_system import HOOK_LOCAL_MENU, ModuleContext

from .tools import PcControlTools

DESCRIPTION = "управление ПК Создателя: терминал, файлы, буфер обмена, локальное меню"
VERSION = "1.0"
DEFAULT_ENABLED = True

logger = logging.getLogger("Ada.Module.pc_control")

__all__ = ["PcControlTools", "setup"]


def setup(ctx: ModuleContext) -> None:
    pc_tools = PcControlTools(pc_allowed=ctx.tools._pc_allowed if ctx.tools else (lambda: False))

    ctx.register_tool(
        "execute_system_command",
        pc_tools.execute_system_command,
        "Выполнить системную команду в PowerShell/CMD/Bash (только для Создателя).",
        {"command": {"type": "string"}},
        ["command"],
    )
    ctx.register_tool(
        "read_file",
        pc_tools.read_file,
        "Прочитать текстовый файл с диска.",
        {"path": {"type": "string"}},
        ["path"],
    )
    ctx.register_tool(
        "write_file",
        pc_tools.write_file,
        "Записать файл на диск (только для Создателя).",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    )
    ctx.register_tool(
        "list_directory",
        pc_tools.list_directory,
        "Посмотреть файлы и папки в каталоге.",
        {"path": {"type": "string"}},
        [],
    )
    ctx.register_tool(
        "open_on_pc",
        pc_tools.open_on_pc,
        "ПК Создателя: открыть сайт (URL), ярлык с рабочего стола (по имени), файл или папку. "
        "app='vscode' — открыть папку/файл в VS Code. Только Создатель.",
        {"target": {"type": "string"}, "app": {"type": "string"}},
        ["target"],
    )
    ctx.register_tool(
        "open_terminal",
        pc_tools.open_terminal,
        "ПК Создателя: открыть видимое окно терминала PowerShell в папке cwd и сразу выполнить "
        "command (необязательно). Только Создатель.",
        {"command": {"type": "string"}, "cwd": {"type": "string"}},
        [],
    )
    ctx.register_tool(
        "clipboard",
        pc_tools.clipboard,
        "ПК Создателя: буфер обмена. action='get' — прочитать текст, 'set' — записать text. "
        "Только Создатель.",
        {"action": {"type": "string", "enum": ["get", "set"]}, "text": {"type": "string"}},
        ["action"],
    )
    ctx.register_tool(
        "show_on_desktop",
        pc_tools.show_on_desktop,
        "ПК Создателя: показать текст в окне на рабочем столе (в Telegram не отправляется). "
        "Только Создатель.",
        {"text": {"type": "string"}},
        ["text"],
    )
    ctx.register_capability(
        "execute_system_command / read_file / write_file / list_directory / open_on_pc / "
        "open_terminal / clipboard / show_on_desktop: управление ПК Создателя (только Создатель в личке)."
    )

    def start_local_menu(on_submit, transcribe) -> bool:
        if __import__("os").name != "nt":
            return False
        from . import overlay
        overlay.start(on_submit, transcribe)
        return True

    ctx.register_hook(HOOK_LOCAL_MENU, start_local_menu)
