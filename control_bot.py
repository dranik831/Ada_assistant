"""Telegram Bot API controller. Does not import or start Ada on its own."""
import asyncio
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, Message
from dotenv import dotenv_values

from control_channel import request
from process_lock import InstanceRunning, instance_lock

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = ROOT / "logs" / "ada.log"
LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
LOG_HEADER = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \[(\w+)\] \[")
MENU = (
    "/status · состояние Ады\n/start_ada · запустить\n/restart · перезапустить\n"
    "/stop · остановить\n/edit · редактирование\n"
    "/logs INFO WARNING ERROR · получать новые логи выбранных уровней\n"
    "/logs off · отключить логи"
)


class AdaProcess:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.child = None
        self.secret = ""

    @staticmethod
    def external_running():
        try:
            with instance_lock(DATA / "ada.lock"):
                return False
        except InstanceRunning:
            return True

    def alive(self):
        return self.child is not None and self.child.returncode is None

    async def call(self, command, actor=0, timeout=5):
        if not self.alive():
            raise ConnectionError("Ada is not running")
        port = int(self.endpoint.read_text(encoding="ascii"))
        return await request(port, self.secret, command, actor, timeout)

    async def status(self):
        if self.alive():
            try:
                state = await self.call("status", timeout=1)
            except (OSError, ValueError, TimeoutError):
                return f"Процесс Ады запущен (PID {self.child.pid}), канал управления пока не отвечает."
            label = {"ready": "Ада в сети", "starting": "Ада запускается", "stopping": "Ада останавливается"}.get(state, "Ада запущена")
            return f"{label} (PID {self.child.pid})."
        if self.external_running():
            return "Ада запущена вне этого бота. Бот не будет останавливать этот процесс."
        if self.child is not None:
            return f"Ада остановлена. Код последнего выхода: {self.child.returncode}."
        return "Ада не запущена."

    async def start(self):
        if self.alive() or self.external_running():
            return await self.status()
        self.endpoint.unlink(missing_ok=True)
        self.secret = secrets.token_hex(32)
        env = os.environ.copy()
        env.update(ADA_CONTROL_ENDPOINT=str(self.endpoint), ADA_CONTROL_SECRET=self.secret)
        env.pop("CONTROL_BOT_TOKEN", None)
        self.child = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(ROOT / "main.py"), cwd=ROOT, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return f"Запускаю Аду (PID {self.child.pid}). Готовность: /status."

    async def stop(self):
        if not self.alive():
            return await self.status()
        try:
            await self.call("stop", timeout=2)
            await asyncio.wait_for(self.child.wait(), 10)
        except (OSError, ValueError, TimeoutError):
            if self.alive():
                try:
                    self.child.terminate()
                except ProcessLookupError:
                    pass
                await self.child.wait()
                return "Процесс Ады остановлен принудительно."
        return "Ада остановлена. Бот продолжает работать."


class LogTail:
    def __init__(self, path):
        self.path = path
        self.identity = None
        self.offset = 0
        self.level = None
        self.pending = b""
        try:
            stat = path.stat()
            self.identity = (stat.st_dev, stat.st_ino)
            self.offset = stat.st_size
        except FileNotFoundError:
            pass

    def read(self):
        try:
            with self.path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if identity != self.identity or stat.st_size < self.offset:
                    self.identity, self.offset, self.level, self.pending = identity, 0, None, b""
                handle.seek(self.offset)
                data = handle.read(65536)
                self.offset = handle.tell()
        except FileNotFoundError:
            return []
        *lines, self.pending = (self.pending + data).split(b"\n")
        self.pending = self.pending[-65536:]
        result = []
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            match = LOG_HEADER.match(line)
            if match:
                self.level = match[1]
            if self.level in LEVELS and line:
                result.append((self.level, line))
        return result


async def relay_logs(bot, subscriptions, token):
    tail = LogTail(LOG)
    while True:
        try:
            rows = await asyncio.to_thread(tail.read)
            if rows and subscriptions:
                env = dotenv_values(ROOT / ".env")
                secret_values = {token}
                secret_values.update(
                    value for key, value in (env | os.environ).items()
                    if value and len(value) >= 6 and key.endswith(("_KEY", "_TOKEN", "_PASSWORD", "_SECRET", "_HASH"))
                )
                for user, levels in tuple(subscriptions.items()):
                    selected = [line for level, line in rows if level in levels]
                    if not selected:
                        continue
                    text = "\n".join(selected)
                    for value in sorted(secret_values, key=len, reverse=True):
                        text = text.replace(value, "[скрыто]")
                    if len(text) > 1850:
                        text = text[:1850] + "\n[часть логов пропущена]"
                    try:
                        await bot.send_message(user, text)
                    except TelegramAPIError as exc:
                        logging.warning("Не удалось отправить лог пользователю %s (%s).", user, type(exc).__name__)
        except OSError:
            logging.warning("Не удалось прочитать лог Ады.")
        await asyncio.sleep(2)


def make_dispatcher(allowed, ada, subscriptions):
    dp = Dispatcher()
    editing = set()
    lifecycle = asyncio.Lock()

    @dp.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def handle(message: Message):
        actor = message.from_user
        if actor is None or actor.id not in allowed or actor.is_bot or message.date.timestamp() < started_at:
            return
        text = message.text.strip()
        if not text:
            return
        command, *args = text.split()
        command = command.split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help"}:
                reply = MENU
            elif command in {"/status", "состояние"}:
                reply = await ada.status()
                if command == "состояние" and actor.id in editing and ada.alive():
                    reply += "\n\n" + await ada.call(text, actor.id)
            elif command in {"/start_ada", "/restart", "/stop", "запуск", "перезапуск", "остановка"}:
                async with lifecycle:
                    if command in {"/restart", "перезапуск"} and ada.alive():
                        await ada.stop()
                        await asyncio.sleep(2)
                    reply = await (ada.stop() if command in {"/stop", "остановка"} else ada.start())
            elif command == "/logs":
                selection = {word.upper() for word in re.split(r"[\s,]+", " ".join(args)) if word}
                if not selection:
                    reply = "Подписка: " + (", ".join(sorted(subscriptions.get(actor.id, ()))) or "выключена")
                    reply += "\n/logs DEBUG INFO WARNING ERROR CRITICAL или /logs off"
                elif selection == {"OFF"}:
                    subscriptions.pop(actor.id, None)
                    reply = "Подписка выключена."
                elif selection <= LEVELS:
                    subscriptions[actor.id] = selection
                    reply = "Уровни логов: " + ", ".join(sorted(selection))
                else:
                    reply = "Доступны DEBUG, INFO, WARNING, ERROR, CRITICAL; /logs off отключает подписку."
            elif command in {"/edit", "редактирование"}:
                editing.add(actor.id)
                reply = await ada.call("меню", actor.id) if ada.alive() else "Режим редактирования включён. Для команд сначала /start_ada."
            elif command in {"/exit", "выйти"}:
                editing.discard(actor.id)
                reply = "Режим редактирования выключен."
            elif actor.id in editing:
                reply = await ada.call(text, actor.id, timeout=65) if ada.alive() else await ada.status()
            else:
                reply = MENU
        except (OSError, ValueError, TimeoutError) as exc:
            logging.warning("Не удалось выполнить команду управления (%s).", type(exc).__name__)
            reply = "Ада не ответила или ещё запускается. Проверь /status."
        # Telegram limits UTF-16 length, including supplementary characters.
        for offset in range(0, len(reply), 1900):
            await message.answer(reply[offset:offset + 1900])

    started_at = int(time.time())
    return dp


async def main():
    settings = dotenv_values(ROOT / ".env")
    token = (settings.get("CONTROL_BOT_TOKEN") or "").strip()
    try:
        allowed = {int(value.strip()) for value in (settings.get("CONTROL_BOT_ALLOWED_IDS") or "").split(",") if value.strip()}
        if not allowed or min(allowed) <= 0:
            raise ValueError
    except ValueError:
        raise SystemExit("Заполни CONTROL_BOT_ALLOWED_IDS в .env: положительные ID через запятую.")
    if not token:
        raise SystemExit("Вставь токен BotFather в CONTROL_BOT_TOKEN в .env.")
    with tempfile.TemporaryDirectory(prefix="ada-control-", dir=DATA) as tmp:
        ada = AdaProcess(Path(tmp) / "port")
        subscriptions = {}
        dp = make_dispatcher(allowed, ada, subscriptions)
        async with Bot(token) as bot:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_my_commands([BotCommand(command=cmd, description=label) for cmd, label in (
                ("start", "Меню"), ("status", "Состояние Ады"), ("start_ada", "Запустить Аду"),
                ("restart", "Перезапустить Аду"), ("stop", "Остановить Аду"),
                ("edit", "Режим редактирования"), ("logs", "Подписка на логи"),
            )])
            relay = asyncio.create_task(relay_logs(bot, subscriptions, token))
            try:
                await dp.start_polling(bot, allowed_updates=["message"], close_bot_session=False, handle_as_tasks=True)
            finally:
                relay.cancel()
                await asyncio.gather(relay, return_exceptions=True)
                if ada.alive():
                    await ada.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        with instance_lock(DATA / "control_bot.lock"):
            asyncio.run(main())
    except InstanceRunning:
        raise SystemExit("Бот управления уже запущен.")
    except (TelegramAPIError, ValueError) as exc:
        raise SystemExit(f"Не удалось запустить бота ({type(exc).__name__}). Проверь токен и доступ к Telegram Bot API.") from None
