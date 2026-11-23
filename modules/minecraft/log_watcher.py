"""Следит за logs/latest.log сервера Minecraft и достаёт сообщения чата.

Формат строки лога (vanilla/Forge/NeoForge, разные логгеры добавляют свой поток):
    [21:14:33] [Server thread/INFO]: <Nick> привет всем
Сообщения модов (например системные команды) логируются иначе и сюда не попадают —
парсер ищет только человеческие реплики в угловых скобках после INFO.
"""
import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger("Ada.Module.minecraft")

# "<Nick> text" — ровно так Minecraft пишет обычный чат в лог.
_CHAT_RE = re.compile(r"\[Server thread/INFO\]:\s*<([^>]+)>\s*(.*)$")
# Игрок присоединился/вышел — полезно для автономных реплик вроде приветствия.
_JOIN_RE = re.compile(r"\[Server thread/INFO\]:\s*(\S+) joined the game")
_LEAVE_RE = re.compile(r"\[Server thread/INFO\]:\s*(\S+) left the game")


def parse_line(line: str) -> dict | None:
    """Возвращает {"kind": "chat"|"join"|"leave", "player": str, "text": str} или None."""
    match = _CHAT_RE.search(line)
    if match:
        return {"kind": "chat", "player": match.group(1), "text": match.group(2).strip()}
    match = _JOIN_RE.search(line)
    if match:
        return {"kind": "join", "player": match.group(1), "text": ""}
    match = _LEAVE_RE.search(line)
    if match:
        return {"kind": "leave", "player": match.group(1), "text": ""}
    return None


class LogWatcher:
    """Читает новые строки из конца файла; переживает поворот лога (новый latest.log)."""

    def __init__(self, log_path: Path, poll_seconds: float = 1.0):
        self.log_path = log_path
        self.poll_seconds = poll_seconds
        self._running = False
        self._position = 0
        self._inode_size = -1  # размер на момент последнего чтения — грубая метка ротации

    async def start(self, on_event):
        """on_event(dict) вызывается для каждой распознанной строки; может быть async."""
        self._running = True
        # Начинаем с конца файла — старые сообщения из прошлых сессий не нужны.
        if self.log_path.exists():
            self._position = self.log_path.stat().st_size
        asyncio.create_task(self._loop(on_event))

    def stop(self):
        self._running = False

    async def _loop(self, on_event):
        while self._running:
            try:
                await self._poll_once(on_event)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("Ошибка чтения лога Minecraft")
            await asyncio.sleep(self.poll_seconds)

    async def _poll_once(self, on_event):
        if not self.log_path.exists():
            return
        size = self.log_path.stat().st_size
        if size < self._position:
            # Файл стал меньше — значит сервер перезапустился и начал новый лог.
            self._position = 0
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._position)
            lines = f.readlines()
            self._position = f.tell()
        for line in lines:
            event = parse_line(line)
            if event is None:
                continue
            try:
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Ошибка обработки события Minecraft-лога")
