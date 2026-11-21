"""Инструменты управления ПК Создателя: терминал, файлы, буфер обмена, запуск программ.

Доступ только Создателю и только в его личке/локальном меню (проверка _pc_allowed
из основного AgentTools), чтобы в группе с пакетом сообщений от нескольких людей
никто другой не смог выполнить команду.
"""

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

import config

logger = logging.getLogger("Ada.Module.pc_control")


def _find_on_desktop(name: str):
    """Ищет ярлык/файл на рабочем столе пользователя и общем рабочем столе по части имени."""
    wanted = name.lower().removesuffix(".lnk").removesuffix(".url")
    desktops = [Path.home() / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]
    items = [item for d in desktops if d.is_dir() for item in d.iterdir()]
    exact = [item for item in items if item.stem.lower() == wanted]
    partial = [item for item in items if wanted in item.stem.lower()]
    return (exact or partial or [None])[0]


class PcControlTools:
    """Методы, которые модуль регистрирует как инструменты для модели."""

    def __init__(self, pc_allowed):
        # pc_allowed: () -> bool — проверка «Создатель и в личке», приходит из ядра.
        self._pc_allowed = pc_allowed

    async def execute_system_command(self, command: str) -> str:
        """Выполняет системную команду в PowerShell/Bash. Доступно только Создателю."""
        if not self._pc_allowed():
            return "Ошибка: Доступ к терминалу заблокирован политикой безопасности. Команда доступна только Создателю в личном чате."

        if config.SYSTEM_COMMAND_ALLOWLIST:
            first_token = command.strip().split(maxsplit=1)[0].strip('"\'').lower()
            allowed = any(
                first_token == entry or first_token.endswith(entry) or first_token.startswith(entry)
                for entry in config.SYSTEM_COMMAND_ALLOWLIST
            )
            if not allowed:
                return (
                    f"Команда '{first_token}' не входит в белый список разрешённых программ "
                    f"({', '.join(config.SYSTEM_COMMAND_ALLOWLIST)})."
                )

        logger.info(f"Исполнение терминальной команды: {command}")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)

            out_decoded = stdout.decode('cp1251', errors='replace') if os.name == 'nt' else stdout.decode('utf-8', errors='replace')
            err_decoded = stderr.decode('cp1251', errors='replace') if os.name == 'nt' else stderr.decode('utf-8', errors='replace')

            res = []
            if out_decoded.strip():
                res.append(f"STDOUT:\n{out_decoded.strip()}")
            if err_decoded.strip():
                res.append(f"STDERR:\n{err_decoded.strip()}")
            if not res:
                res.append("Команда выполнена успешно (вывод пуст).")

            output = "\n".join(res)
            return output[:3000]
        except asyncio.TimeoutError:
            return "Таймаут выполнения команды (превышено 45 секунд)."
        except Exception as e:
            return f"Ошибка запуска процесса: {e}"

    async def open_on_pc(self, target: str, app: str = "") -> str:
        """Открывает на ПК сайт, ярлык с рабочего стола, файл или папку; app='vscode' — в VS Code."""
        if not self._pc_allowed():
            return "Управление компьютером доступно только Создателю в личном чате."
        target = (target or "").strip().strip('"')
        if not target:
            return "Не указано, что открыть."
        try:
            if target.lower().startswith(("http://", "https://")):
                webbrowser.open(target)
                return f"Открыла в браузере: {target}"
            path = Path(os.path.expandvars(os.path.expanduser(target)))
            if not path.exists():
                path = _find_on_desktop(target)
                if path is None:
                    return f"Не нашла «{target}»: это не URL, не существующий путь и не ярлык на рабочем столе."
            if app.strip().lower() in {"vscode", "code", "vs code"}:
                # URI-схема VS Code: без cmd-обёртки code.cmd и без разбора аргументов шеллом.
                os.startfile("vscode://file/" + path.resolve().as_posix())
                return f"Открыла в VS Code: {path}"
            os.startfile(str(path))
            return f"Открыла: {path}"
        except OSError as e:
            return f"Не удалось открыть «{target}»: {e}"

    async def open_terminal(self, command: str = "", cwd: str = "") -> str:
        """Открывает видимое окно терминала (PowerShell), при необходимости сразу с командой."""
        if not self._pc_allowed():
            return "Управление компьютером доступно только Создателю в личном чате."
        workdir = Path(os.path.expandvars(os.path.expanduser(cwd))) if cwd else Path.home()
        if not workdir.is_dir():
            return f"Папка не найдена: {cwd}"
        args = ["powershell.exe", "-NoExit"] + (["-Command", command] if command else [])
        try:
            if shutil.which("wt"):
                subprocess.Popen(["wt", "-d", str(workdir)] + args)
            else:
                subprocess.Popen(args, cwd=workdir, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as e:
            return f"Не удалось открыть терминал: {e}"
        logger.info("Открыт терминал в %s: %s", workdir, command)
        return f"Открыла терминал в {workdir}" + (f" с командой: {command}" if command else "") + "."

    async def clipboard(self, action: str = "get", text: str = "") -> str:
        """Буфер обмена Windows: action='get' — прочитать, 'set' — записать text."""
        if not self._pc_allowed():
            return "Управление компьютером доступно только Создателю в личном чате."
        if action == "set":
            # Текст передаётся в base64: никаких кавычек и подстановок внутри команды PowerShell.
            payload = base64.b64encode(text.encode("utf-8")).decode()
            script = f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')))"
        elif action == "get":
            script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"
        else:
            return "action должен быть get или set."
        process = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-Command", script,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode:
            return f"Ошибка буфера обмена: {stderr.decode('utf-8', errors='replace')[:500]}"
        if action == "set":
            return f"Скопировала в буфер обмена ({len(text)} символов)."
        content = stdout.decode("utf-8", errors="replace").strip()
        return f"В буфере обмена:\n{content[:3000]}" if content else "Буфер обмена пуст (или там не текст)."

    async def read_file(self, path: str) -> str:
        """Читает текстовый файл по указанному пути."""
        if not self._pc_allowed():
            return "Доступ к файлам компьютера есть только у Создателя в личном чате."
        try:
            target = Path(path).resolve()
            if not target.exists():
                return f"Файл не найден: {path}"
            if not target.is_file():
                return f"Путь указывает на папку, а не файл: {path}"

            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(4000)
            return f"Содержимое {target.name}:\n{content}"
        except Exception as e:
            return f"Не удалось прочитать файл: {e}"

    async def write_file(self, path: str, content: str) -> str:
        """Записывает текст в файл (только Создатель)."""
        if not self._pc_allowed():
            return "Запись файлов разрешена только Создателю в личном чате."
        try:
            target = Path(path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Файл {target.name} успешно сохранен."
        except Exception as e:
            return f"Ошибка записи файла: {e}"

    async def list_directory(self, path: str = ".") -> str:
        """Возвращает список файлов и папок в директории."""
        if not self._pc_allowed():
            return "Доступ к файлам компьютера есть только у Создателя в личном чате."
        try:
            p = Path(path).resolve()
            if not p.exists():
                return f"Папка не найдена: {path}"
            items = []
            for item in list(p.iterdir())[:30]:
                prefix = "[DIR] " if item.is_dir() else "[FILE]"
                items.append(f"{prefix} {item.name}")
            return f"Каталог {p.name}:\n" + "\n".join(items)
        except Exception as e:
            return f"Не удалось открыть каталог: {e}"

    async def show_on_desktop(self, text: str) -> str:
        """Показывает текст в окне оверлея на ПК Создателя."""
        if not self._pc_allowed():
            return "Вывод на экран ПК доступен только Создателю в личном чате."
        # overlay импортирует tkinter; не импортируем сами — если окно не запущено, модуля нет в sys.modules.
        overlay = sys.modules.get("modules.pc_control.overlay")
        if overlay is None or overlay._events is None:
            return "Окно на рабочем столе не запущено."
        overlay.show_reply(text)
        return "Показала на экране."
