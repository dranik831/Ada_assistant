"""Модуль Minecraft: RCON-команды и чтение игрового чата через logs/latest.log.

Включение: MODULE_MINECRAFT=true в .env.

Настройки .env:
  MINECRAFT_RCON_HOST=127.0.0.1
  MINECRAFT_RCON_PORT=25575
  MINECRAFT_RCON_PASSWORD=
  MINECRAFT_SERVER_DIR=            # корневая папка сервера; логи ищутся в <dir>/logs/latest.log
  MINECRAFT_LOG_PATH=              # полный путь к latest.log вместо MINECRAFT_SERVER_DIR
  MINECRAFT_CHAT_REPLY_PROBABILITY=0.3   # шанс автономно вступить в игровой чат
  MINECRAFT_CHAT_COOLDOWN_SECONDS=20     # минимальный перерыв между автономными репликами

Чат игроков подключён так же, как группы Telegram: пакет из нескольких
сообщений идёт в brain.process_interaction с отрицательным chat_id, ответ
уходит в игру через RCON `say`. Команды через send_minecraft_command не
ограничены — RCON сам по себе требует отдельного пароля и доступен только
тому, кто его настроил.

Даёт инструменты:
  send_minecraft_command — выполнить любую команду на сервере (только Создатель);
  send_minecraft_chat     — написать в игровой чат от имени Ады;
хуки:
  startup/shutdown — запускает/останавливает слежение за логом.
"""

import asyncio
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from module_system import HOOK_SHUTDOWN, HOOK_STARTUP, ModuleContext

import config

from .log_watcher import LogWatcher
from .rcon import RconClient, RconError

DESCRIPTION = "Minecraft: RCON-команды и автономное общение в игровом чате"
VERSION = "1.0"
DEFAULT_ENABLED = False

logger = logging.getLogger("Ada.Module.minecraft")

# Диапазон псевдо-ID для игроков Minecraft: далеко от реальных Telegram ID
# (положительные десятизначные+ числа), чтобы таблицы users/messages не
# пересеклись при общем хранилище.
_PLAYER_ID_BASE = 900_000_000_000_000

__all__ = ["setup"]


def _player_user_id(nickname: str) -> int:
    """Стабильный псевдо-ID игрока для таблиц users/messages (детерминирован по нику)."""
    digest = abs(hash(nickname.lower())) % 1_000_000_000
    return _PLAYER_ID_BASE + digest


def setup(ctx: ModuleContext) -> None:
    host = config_get("MINECRAFT_RCON_HOST", "127.0.0.1")
    port = int(config_get("MINECRAFT_RCON_PORT", "25575"))
    password = config_get("MINECRAFT_RCON_PASSWORD", "")
    rcon = RconClient(host, port, password)

    chat_chat_id = -_PLAYER_ID_BASE  # условный "chat_id" игрового чата для истории/памяти
    reply_probability = float(config_get("MINECRAFT_CHAT_REPLY_PROBABILITY", "0.3"))
    reply_cooldown = float(config_get("MINECRAFT_CHAT_COOLDOWN_SECONDS", "20"))

    log_path_str = config_get("MINECRAFT_LOG_PATH", "")
    server_dir = config_get("MINECRAFT_SERVER_DIR", "")
    if log_path_str:
        log_path = Path(log_path_str)
    elif server_dir:
        log_path = Path(server_dir) / "logs" / "latest.log"
    else:
        log_path = None

    watcher = LogWatcher(log_path) if log_path else None
    state = {"last_reply_at": 0.0, "batch": [], "batch_task": None}

    async def send_minecraft_command(command: str) -> str:
        if not ctx.tools or not ctx.tools._is_admin():
            return "Выполнять команды на сервере Minecraft может только Создатель."
        try:
            return await rcon.execute(command) or "Команда выполнена (ответ пуст)."
        except RconError as exc:
            return f"Ошибка RCON: {exc}"
        except Exception as exc:
            return f"Не удалось выполнить команду: {type(exc).__name__}: {exc}"

    async def send_minecraft_chat(message: str) -> str:
        try:
            # tellraw безопаснее say: не интерпретирует содержимое как команду
            # и не ломается на кавычках так легко, как голый say.
            safe = message.replace('"', "'")[:500]
            await rcon.execute(f'tellraw @a {{"text":"<Ада> {safe}"}}')
            return "Отправила в игровой чат."
        except RconError as exc:
            return f"Ошибка RCON: {exc}"
        except Exception as exc:
            return f"Не удалось отправить сообщение: {type(exc).__name__}: {exc}"

    async def deliver_reply(text: str) -> None:
        try:
            await rcon.execute(f'tellraw @a {{"text":"<Ада> {text.replace(chr(34), chr(39))[:500]}"}}')
        except Exception:
            logger.exception("Не удалось отправить ответ в игровой чат")

    async def process_batch() -> None:
        items = state["batch"]
        state["batch"] = []
        if not items:
            return
        lines = []
        last_player = None
        last_user_id = None
        for player, text in items:
            user_id = _player_user_id(player)
            last_player, last_user_id = player, user_id
            await ctx.db.get_or_create_user(user_id, username="", first_name=player, is_admin=False)
            row_id = await ctx.db.save_message(
                user_id, "user", text, chat_id=chat_chat_id, sender_name=player,
            )
            lines.append(f"[{datetime.now().strftime('%H:%M')}] {player} [id {user_id}]: {text}")
        incoming_lines = "\n".join(lines)

        brain = ctx.service("brain")
        if brain is None or last_user_id is None:
            return
        decision = await brain.process_interaction(
            last_user_id, incoming_lines, is_admin=False,
            chat_context="[Сообщения из игрового чата Minecraft; это НЕ Telegram. Если отвечаешь — "
                        "коротко, по-игровому, без телеграм-формальностей.]\n",
            chat_id=chat_chat_id,
        )
        if decision.get("error") or not decision.get("must_reply", True):
            return
        reply = "\n".join(p.strip() for p in (decision.get("messages") or []) if p).strip()
        if reply:
            await deliver_reply(reply)
            await ctx.db.save_message(0, "ada", reply, chat_id=chat_chat_id, sender_name="Ада")
            state["last_reply_at"] = time.time()

    def schedule_batch(player: str, text: str) -> None:
        state["batch"].append((player, text))
        if state["batch_task"] and not state["batch_task"].done():
            return

        async def flush():
            await asyncio.sleep(4.0)
            await process_batch()
            state["batch_task"] = None

        state["batch_task"] = asyncio.create_task(flush())

    def may_reply() -> bool:
        if time.time() - state["last_reply_at"] < reply_cooldown:
            return False
        return random.random() < reply_probability

    async def on_log_event(event: dict) -> None:
        if event["kind"] != "chat":
            return
        if not may_reply() and not state["batch"]:
            # Не вступаем в разговор прямо сейчас, но если уже начат пакет — довершаем его.
            return
        schedule_batch(event["player"], event["text"])

    async def on_startup() -> None:
        if watcher is None:
            logger.warning("Minecraft: не задан MINECRAFT_LOG_PATH/MINECRAFT_SERVER_DIR — чат не отслеживается")
            return
        await watcher.start(on_log_event)
        logger.info("Minecraft: слежу за логом %s", watcher.log_path)

    async def on_shutdown() -> None:
        if watcher:
            watcher.stop()

    ctx.register_tool(
        "send_minecraft_command",
        send_minecraft_command,
        "Выполнить любую команду на сервере Minecraft через RCON (например бан, выдача предметов, "
        "модовые команды). Только Создатель.",
        {"command": {"type": "string"}},
        ["command"],
    )
    ctx.register_tool(
        "send_minecraft_chat",
        send_minecraft_chat,
        "Написать сообщение в игровой чат Minecraft от своего имени.",
        {"message": {"type": "string"}},
        ["message"],
    )
    ctx.register_hook(HOOK_STARTUP, on_startup)
    ctx.register_hook(HOOK_SHUTDOWN, on_shutdown)
    ctx.register_capability(
        "send_minecraft_command / send_minecraft_chat: управление сервером Minecraft через RCON "
        "и общение в игровом чате; реагирую на игроков автономно, как в группах Telegram."
    )


def config_get(name: str, default: str) -> str:
    import os
    return os.getenv(name, default).strip()
