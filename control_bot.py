"""Telegram Bot API controller. Does not import or start Ada on its own."""
import asyncio
import ipaddress
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from dotenv import dotenv_values

from control_channel import request
from control_web import EnvEditor, make_app, parse_ids
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
        self.lifecycle = asyncio.Lock()

    async def manage(self, action, actor):
        async with self.lifecycle:
            if action == "maintenance":
                return await self.call("техработы", actor) if self.alive() else await self.status()
            if action == "restart" and self.alive():
                await self.stop()
                await asyncio.sleep(2)
            return await (self.stop() if action == "stop" else self.start())

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
    def __init__(self, path, history_bytes=0):
        self.path = path
        self.identity = None
        self.offset = 0
        self.level = None
        self.pending = b""
        try:
            stat = path.stat()
            self.identity = (stat.st_dev, stat.st_ino)
            self.offset = max(0, stat.st_size - history_bytes)
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


async def relay_logs(bot, subscriptions, token, allowed, history):
    tail = LogTail(LOG, history_bytes=65536)
    initial = True
    while True:
        try:
            rows = await asyncio.to_thread(tail.read)
            if rows:
                env = dotenv_values(ROOT / ".env", interpolate=False)
                secret_values = {token}
                secret_values.update(
                    value for key, value in (env | os.environ).items()
                    if value and len(value) >= 4 and re.search(r"KEY|TOKEN|PASSWORD|SECRET|HASH|PHONE|CREDENTIAL|COOKIE|SESSION", key, re.I)
                )
                for value in sorted(secret_values, key=len, reverse=True):
                    rows = [(level, line.replace(value, "[скрыто]")) for level, line in rows]
                history.extend((level, line[:2000]) for level, line in rows[-300:])
                permitted = allowed()
                for user, levels in tuple(subscriptions.items()):
                    if initial:
                        break
                    if user not in permitted:
                        subscriptions.pop(user, None)
                        continue
                    selected = [line for level, line in rows if level in levels]
                    if not selected:
                        continue
                    text = "\n".join(selected)
                    if len(text) > 1850:
                        text = text[:1850] + "\n[часть логов пропущена]"
                    try:
                        await bot.send_message(user, text)
                    except TelegramAPIError as exc:
                        logging.warning("Не удалось отправить лог пользователю %s (%s).", user, type(exc).__name__)
        except OSError:
            logging.warning("Не удалось прочитать лог Ады.")
        initial = False
        await asyncio.sleep(2)


def make_dispatcher(allowed, ada, subscriptions, miniapp_url=""):
    dp = Dispatcher()
    editing = set()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Открыть панель", web_app=WebAppInfo(url=miniapp_url))]]) if miniapp_url else None

    @dp.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def handle(message: Message):
        actor = message.from_user
        if actor is None or actor.id not in allowed() or actor.is_bot or message.date.timestamp() < started_at:
            return
        text = message.text.strip()
        if not text:
            return
        command, *args = text.split()
        command = command.split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help", "/panel"}:
                await message.answer(MENU, reply_markup=keyboard)
                return
            elif command in {"/status", "состояние"}:
                reply = await ada.status()
                if command == "состояние" and actor.id in editing and ada.alive():
                    reply += "\n\n" + await ada.call(text, actor.id)
            elif command in {"/start_ada", "/restart", "/stop", "запуск", "перезапуск", "остановка"}:
                action = {"/restart": "restart", "перезапуск": "restart", "/stop": "stop", "остановка": "stop"}.get(command, "start")
                reply = await ada.manage(action, actor.id)
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
    settings = dotenv_values(ROOT / ".env", interpolate=False)
    token = (settings.get("CONTROL_BOT_TOKEN") or "").strip()
    try:
        allowed = parse_ids(settings.get("CONTROL_BOT_ALLOWED_IDS"))
    except ValueError:
        raise SystemExit("Заполни CONTROL_BOT_ALLOWED_IDS в .env: положительные ID через запятую.")
    if not token:
        raise SystemExit("Вставь токен BotFather в CONTROL_BOT_TOKEN в .env.")
    url = (settings.get("CONTROL_WEB_URL") or "").strip()
    bind = ipaddress.IPv4Address(settings.get("CONTROL_WEB_BIND") or "127.0.0.1")
    peer = settings.get("CONTROL_WEB_PROXY_PEER") or None
    if url and not bind.is_loopback and (not bind.is_private or bind.is_unspecified or not peer):
        raise SystemExit("Для LAN укажи локальный CONTROL_WEB_BIND и CONTROL_WEB_PROXY_PEER.")
    if peer:
        peer = str(ipaddress.IPv4Address(peer))
    if url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise SystemExit("CONTROL_WEB_URL должен быть публичным HTTPS-адресом на корне домена.")
    owner = int(settings.get("ADMIN_USER_ID") or 0)
    if url and owner not in allowed:
        raise SystemExit("Для Mini App укажи ADMIN_USER_ID и добавь владельца в CONTROL_BOT_ALLOWED_IDS.")
    env = EnvEditor(ROOT / ".env", ROOT / ".env.example", owner)
    with tempfile.TemporaryDirectory(prefix="ada-control-", dir=DATA) as tmp:
        ada = AdaProcess(Path(tmp) / "port")
        subscriptions = {}
        history = deque(maxlen=300)
        dp = make_dispatcher(env.allowed, ada, subscriptions, url)
        async with Bot(token) as bot:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_my_commands([BotCommand(command=cmd, description=label) for cmd, label in (
                ("start", "Меню"), ("status", "Состояние Ады"), ("start_ada", "Запустить Аду"),
                ("restart", "Перезапустить Аду"), ("stop", "Остановить Аду"),
                ("edit", "Режим редактирования"), ("logs", "Подписка на логи"),
            )] + ([BotCommand(command="panel", description="Открыть панель")] if url else []))
            runner = None
            relay = asyncio.create_task(relay_logs(bot, subscriptions, token, env.allowed, history))
            try:
                if url:
                    app = make_app(token=token, env=env, ada=ada, logs=history, assets=ROOT / "control_ui", levels=LEVELS, proxy_peer=peer)
                    runner = web.AppRunner(app, access_log=None)
                    await runner.setup()
                    await web.TCPSite(runner, str(bind), int(settings.get("CONTROL_WEB_PORT") or 8765)).start()
                await dp.start_polling(bot, allowed_updates=["message"], close_bot_session=False, handle_as_tasks=True)
            finally:
                if runner:
                    await runner.cleanup()
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
