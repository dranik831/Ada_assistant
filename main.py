import sys
import atexit
import os
import asyncio
import logging
import io
import re
import json
import random
from collections import defaultdict
from datetime import datetime
from contextlib import AsyncExitStack
from pathlib import Path
from telethon import TelegramClient, events, errors
from telethon.tl.types import User, Channel, Chat
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

import config
from database import Database
from emotions import EmotionSystem
from personality import PersonalityManager
from tools import AgentTools
from llm import UniversalAgent
from brain import Brain, format_message_line
import module_system
from autonomy import AutonomyEngine
from control_channel import serve
from control_edit import make_editor
from process_lock import InstanceRunning, instance_lock

logging.basicConfig(
    level=os.getenv("ADA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOGS_DIR / "ada.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("Ada.Main")

if __name__ == "__main__":
    process_lock = instance_lock(config.DATA_DIR / "ada.lock")
    try:
        process_lock.__enter__()
    except InstanceRunning:
        logger.error("Ада уже запущена.")
        sys.exit(2)
    atexit.register(process_lock.__exit__, None, None, None)

db = Database()
emotions = EmotionSystem(db)
personality = PersonalityManager()

client_kwargs = {"proxy": config.TG_PROXY}
if config.TG_CONNECTION_MODE == "mtproxy":
    client_kwargs["connection"] = ConnectionTcpMTProxyRandomizedIntermediate

client = TelegramClient(
    str(config.SESSION_PATH),
    int(config.TG_API_ID) if config.TG_API_ID else 0,
    config.TG_API_HASH,
    **client_kwargs,
)

tools = AgentTools(db, client, emotions)

# Модули из папки modules/ подключаются до создания агента: их инструменты
# попадают в тот же реестр, который агент отдаёт модели.
modules = module_system.load_modules(db=db, client=client, tools=tools, emotions=emotions)

agent = UniversalAgent(tools)
brain = Brain(db, emotions, personality, agent)
# brain зависит от agent, который зависит от инструментов уже собранных модулей —
# поэтому его нельзя передать в load_modules() напрямую; модули, которым он нужен
# (например minecraft), читают его лениво через ctx.service("brain").
modules.set_service("brain", brain)

async def send_user_message(target_id: int, text: str):
    enqueue_message(target_id, text)

autonomy = AutonomyEngine(db, brain, emotions, send_user_message)

_pending_messages: dict[tuple[int, int], dict] = {}

_message_locks = defaultdict(asyncio.Lock)
_group_last_reply: dict[int, float] = {}
_group_batches: dict[int, dict] = {}
GROUP_FOLLOWUP_MESSAGES = 2
_group_followups: dict[int, int] = {}
_group_batch_locks = defaultdict(asyncio.Lock)
_chat_processing_locks = defaultdict(asyncio.Lock)

# Исходящая очередь: все отправки вне обработчиков событий идут через один
# воркер, чтобы разом обрабатывать FloodWait и не плодить параллельные запросы.
_outgoing_queue: asyncio.Queue = asyncio.Queue(maxsize=200)

async def _send_queued(item: dict):
    sent = await client.send_message(item["chat_id"], item["text"])
    if item.get("save", True):
        await db.save_message(0, "ada", item["text"], chat_id=item["chat_id"],
                              sender_name="Ада", tg_message_id=getattr(sent, "id", None))

async def outgoing_worker():
    while True:
        item = await _outgoing_queue.get()
        try:
            await _send_queued(item)
        except errors.FloodWaitError as flood:
            logger.warning("FloodWait %ss перед отправкой в %s", flood.seconds, item["chat_id"])
            await asyncio.sleep(flood.seconds + 1)
            try:
                await _send_queued(item)
            except Exception as retry_error:
                logger.error("Повторная отправка в %s не удалась: %s", item["chat_id"], retry_error)
        except Exception as e:
            logger.error("Ошибка исходящей отправки в %s: %s", item["chat_id"], e)
        finally:
            _outgoing_queue.task_done()

def enqueue_message(chat_id: int, text: str, save: bool = True):
    """save=False — служебное сообщение, в историю диалога не попадает."""
    try:
        _outgoing_queue.put_nowait({"chat_id": chat_id, "text": text[:4096], "save": save})
    except asyncio.QueueFull:
        logger.error("Исходящая очередь переполнена; сообщение в %s потеряно", chat_id)

_LLM_ERROR_NOTICE_INTERVAL = 300

_PRIMARY_SCREEN_REQUEST = re.compile(
    r"(?:скинь|кинь|пришли|отправь|покажи)(?:\s+мне)?\s+(?:скриншот|скрин)"
    r"(?:\s+(?:с|со|основного|главного|экрана|монитора))*[.!?]*", re.IGNORECASE,
)
_last_llm_error_notice = 0.0

async def notify_llm_error(error_text: str, chat_id: int | None, user_id: int):
    """Сообщает Создателю в личку об ошибке LLM API; в контекст не сохраняется."""
    global _last_llm_error_notice
    now = asyncio.get_running_loop().time()
    if _last_llm_error_notice and now - _last_llm_error_notice < _LLM_ERROR_NOTICE_INTERVAL:
        return
    admin_id = config.ADMIN_USER_ID or int(await db.get_kv("admin_id", "0") or 0)
    if not admin_id:
        return
    _last_llm_error_notice = now
    provider_chain = " -> ".join(str(p) for p in agent.providers)
    enqueue_message(
        admin_id,
        f"⚠️ Ошибка LLM API. Все провайдеры в цепочке недоступны: {provider_chain}.\n"
        f"Чат: {chat_id}, собеседник: {user_id}. Ответ не отправлен.\n{error_text[:1500]}\n"
        f"(следующее уведомление не раньше чем через {_LLM_ERROR_NOTICE_INTERVAL // 60} мин)",
        save=False,
    )

brain.on_llm_error = notify_llm_error

async def handle_local_request(text: str) -> str:
    """Запрос из локального меню (Ctrl+Alt+A): идёт как сообщение Создателя, ответ показывается только на экране."""
    admin_id = config.ADMIN_USER_ID or int(await db.get_kv("admin_id", "0") or 0)
    if not admin_id:
        logger.error("Локальное меню: ADMIN_USER_ID не задан, запрос «%s» не обработан", text[:100])
        return "ADMIN_USER_ID не задан в .env — не знаю, кто Создатель."
    name = "Создатель (локальное меню ПК)"
    row_id = await db.save_message(admin_id, "user", text, chat_id=admin_id, sender_name=name)
    line = format_message_line({"user_id": admin_id, "role": "user", "content": text,
                                "timestamp": datetime.now().isoformat(), "sender_name": name}, max_len=4000)
    async with _chat_processing_locks[admin_id]:
        decision = await brain.process_interaction(
            admin_id, line, True, "[Запрос набран в локальном меню на ПК Создателя.]\n",
            chat_id=admin_id, current_message_ids=[row_id],
        )
    if decision.get("error"):
        return f"Ошибка LLM: {decision['error']}"
    replies = filter_silent_messages(decision.get("messages") or []) if decision.get("must_reply", True) else []
    if not replies:
        return "…"
    answer = "\n\n".join(replies)
    # В Telegram не отправляем, но в историю кладём, чтобы Ада помнила разговор.
    await db.save_message(0, "ada", answer, chat_id=admin_id, sender_name="Ада")
    return answer

def start_overlay() -> bool:
    """Локальное меню ПК (Ctrl+Alt+A) предоставляет модуль pc_control; False, если он выключен."""
    loop = asyncio.get_running_loop()

    def on_submit(text: str):
        future = asyncio.run_coroutine_threadsafe(handle_local_request(text), loop)

        def done(f):
            from modules.pc_control import overlay
            if f.exception():
                logger.error("Локальный запрос упал: %s", f.exception())
                overlay.show_reply(f"Что-то сломалось: {f.exception()}")
            else:
                overlay.show_reply(f.result())
        future.add_done_callback(done)

    def transcribe(wav: bytes) -> str:
        future = asyncio.run_coroutine_threadsafe(module_system.transcribe_audio(wav), loop)
        return future.result(timeout=90) or ""

    # Голос появляется в меню только когда модуль voice подключён.
    return module_system.start_local_menu(on_submit, transcribe if module_system.voice_available() else None)

async def send_with_flood_wait(coro_factory):
    """Выполняет отправку, выдерживая FloodWait один раз."""
    try:
        return await coro_factory()
    except errors.FloodWaitError as flood:
        logger.warning("FloodWait %ss; жду и повторяю", flood.seconds)
        await asyncio.sleep(flood.seconds + 1)
        return await coro_factory()

# Маркеры осознанного молчания: такие части ответа не отправляются в чат.
_SILENCE_RE = re.compile(r"^\s*(SILENCE|NO_REPLY|МОЛЧУ|МОЛЧАНИЕ)\s*$", re.IGNORECASE)

def filter_silent_messages(messages) -> list:
    """Убирает маркеры молчания; пустой результат = Ада решает промолчать."""
    return [
        part for part in (messages or [])
        if part and not _SILENCE_RE.match(str(part))
    ]

def _short(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else value[:limit] + "…"

def display_name(entity) -> str:
    if entity is None:
        return "Неизвестно"
    name = " ".join(filter(None, [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]))
    return name or getattr(entity, "title", None) or getattr(entity, "username", None) or str(getattr(entity, "id", "?"))

async def describe_reply(reply_msg, my_id: int) -> str:
    """Кратко описывает сообщение, на которое ответили: автор и текст."""
    if reply_msg.sender_id == my_id:
        author = "Ада (ты)"
    else:
        try:
            author = display_name(await reply_msg.get_sender())
        except Exception:
            author = "Неизвестно"
    label = await module_system.media_label(reply_msg)
    body = f"{label} {reply_msg.text or ''}".strip() or ("[медиа]" if reply_msg.media else "")
    return f"{author}: «{_short(body, 500)}»"

async def collect_message(event, sender_id: int, item: dict) -> list | None:
    """Объединяет быстрые сообщения одного автора в одну реплику.

    item — {"line": оформленная строка, "id": id в БД}; владельцу возвращается список всех item."""
    key = (event.chat_id, sender_id)
    async with _message_locks[key]:
        current = _pending_messages.get(key)
        if current:
            current["items"].append(item)
            current["task"].cancel()
            # Первое событие остаётся владельцем генерации. Последующие события
            # только расширяют его буфер и не запускают второй ответ.
            is_owner = False
        else:
            future = asyncio.get_running_loop().create_future()
            current = {"items": [item], "future": future, "task": None}
            _pending_messages[key] = current
            is_owner = True
            current_future = future

        async def flush():
            try:
                await asyncio.sleep(config.MESSAGE_DEBOUNCE_SECONDS)
                pending = _pending_messages.pop(key, current)
                owner_future = pending["future"]
                if not owner_future.done():
                    owner_future.set_result(pending["items"])
            except asyncio.CancelledError:
                return


        current["task"] = asyncio.create_task(flush())
    if not is_owner:
        return None
    return await current_future

def may_join_group(chat_id: int) -> bool:
    if not config.GROUP_AUTONOMY_ENABLED:
        return False
    now = asyncio.get_running_loop().time()
    last = _group_last_reply.get(chat_id, 0.0)
    if now - last < config.GROUP_REPLY_COOLDOWN_MINUTES * 60:
        return False
    return random.random() < config.GROUP_REPLY_PROBABILITY

async def collect_group_batch(chat_id: int, item: dict) -> list | None:
    """После первого сигнала собирает первое и следующие пять сообщений группы."""
    async with _group_batch_locks[chat_id]:
        batch = _group_batches.get(chat_id)
        if batch:
            batch["items"].append(item)
            if len(batch["items"]) >= 6:
                if not batch["future"].done():
                    batch["future"].set_result(list(batch["items"]))
                _group_batches.pop(chat_id, None)
            return None

        future = asyncio.get_running_loop().create_future()
        batch = {"items": [item], "future": future}
        _group_batches[chat_id] = batch

        async def finish_later():
            await asyncio.sleep(max(4.0, config.MESSAGE_DEBOUNCE_SECONDS + 2.0))
            if not future.done():
                future.set_result(list(batch["items"]))
            if _group_batches.get(chat_id) is batch:
                _group_batches.pop(chat_id, None)

        asyncio.create_task(finish_later())
    return await future

async def check_admin(sender: User) -> bool:
    configured_admin_id = config.ADMIN_USER_ID
    if configured_admin_id:
        # Задан ID — только он. Username можно сменить и его займёт чужой человек.
        return sender.id == configured_admin_id
    saved_admin_id = await db.get_kv("admin_id")
    if (saved_admin_id and str(sender.id) == saved_admin_id) or (
        configured_admin_id and sender.id == configured_admin_id
    ):
        if not saved_admin_id:
            await db.set_kv("admin_id", str(sender.id))
        return True
    
    if sender.username and sender.username.lower() == config.ADMIN_USERNAME.lower():
        await db.set_kv("admin_id", str(sender.id))
        logger.info(f"Администратор @{config.ADMIN_USERNAME} привязан к User ID: {sender.id}")
        return True
    return False

@client.on(events.NewMessage(incoming=True))
async def on_incoming_message(event):
    sender = await event.get_sender()
    
    if not isinstance(sender, User) or sender.bot or sender.is_self:
        return

    text = event.raw_text.strip()
    if event.is_private and re.fullmatch(r"редакт(?:р)?ирование(?:\s+8338)?", text, re.IGNORECASE):
        await event.reply("перенесено в бота")
        return
    if event.message.voice or event.message.audio or event.message.video_note:
        await event.reply("Я не могу прослушивать голосовые и звонки. Напиши это текстом — так будет удобнее.")
        return
    if event.is_private or event.message.mentioned:
        label = await module_system.media_label(event.message)
        text = f"{label} {text}".strip() if label else text
    if not text:
        return

    me = await client.get_me()
    is_admin = await check_admin(sender)

    if (event.is_private and is_admin and not event.fwd_from and modules.tool_specs_for("pc_control")
            and _PRIMARY_SCREEN_REQUEST.fullmatch(event.raw_text.strip())):
        from modules.pc_control.tools import capture_primary_screen

        try:
            shot = await capture_primary_screen()
        except Exception as exc:
            logger.warning("Не удалось захватить основной экран (%s)", type(exc).__name__)
            await event.reply("Не смогла снять основной экран: оба способа захвата не сработали.")
            return
        with io.BytesIO(shot) as image:
            image.name = "primary-screen.png"

            async def send_screenshot():
                image.seek(0)
                return await client.send_file(event.chat_id, image, force_document=True, reply_to=event.id)

            try:
                await send_with_flood_wait(send_screenshot)
            except (OSError, errors.RPCError) as exc:
                logger.warning("Не удалось отправить скриншот (%s)", type(exc).__name__)
                await event.reply("Скриншот сделала, но отправить файл не удалось.")
        return

    remember_match = re.match(r"^запомни\s*[:,-]?\s*(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
    if remember_match:
        scope = "group" if not event.is_private else "person"
        await db.add_scoped_memory(
            scope,
            remember_match.group(1).strip(),
            category="explicit_fact",
            owner_id=sender.id if scope == "person" else None,
            chat_id=event.chat_id if scope == "group" else None,
            importance=8,
            confidence=1.0,
            source=f"explicit_user:{sender.id}",
        )
        await event.reply("Записала дословно в память этого чата.")
        return

    # Регистрация пользователя в локальной БД

    is_new_user = await db.get_user(sender.id) is None
    await db.get_or_create_user(sender.id, sender.username or "", sender.first_name or "", is_admin=is_admin)
    if is_new_user and event.is_private and not is_admin:
        greeting = "Здравствуйте, я секретарь Драника. Чтобы занять время встречи с ним или передать ему что-то, напишите мне."
        await event.reply(greeting)
        await db.save_message(0, "ada", greeting, chat_id=event.chat_id)

    # Триггеры автономности
    if text.lower() in ["разрешить писать первой", "пиши мне", "/optin"]:
        await db.update_user_relationship(sender.id, {"autonomy_allowed": 1})
        await event.reply("Хорошо, теперь я смогу писать тебе первой, когда будет настроение.")
        return
    elif text.lower() in ["не пиши первой", "стоп автономия", "/optout"]:
        await db.update_user_relationship(sender.id, {"autonomy_allowed": 0})
        await event.reply("Поняла. Буду писать только в ответ.")
        return

    # Логика фильтрации сообщений в группах
    should_reply = False
    chat_context = ""
    sender_name = display_name(sender)

    # Сообщение, на которое отвечают, нужно в любом чате, а не только в группе.
    reply_msg = await event.get_reply_message() if event.is_reply else None
    is_reply_to_me = bool(reply_msg and reply_msg.sender_id == me.id)
    reply_desc = await describe_reply(reply_msg, me.id) if reply_msg else ""

    if event.is_private:
        should_reply = True
        await db.upsert_chat(event.chat_id, sender_name, "private")
        if is_new_user:
            chat_context = (
                "[Это первое личное обращение. Секретарское приветствие уже отправлено отдельным сообщением; не повторяй его. "
                "Ответь на конкретную просьбу человека, если она есть.]\n"
            )
    else:
        chat = await event.get_chat()
        chat_title = getattr(chat, 'title', 'Группа')
        await db.upsert_chat(event.chat_id, chat_title, type(chat).__name__)

        clean_text = re.sub(r'[^\w\s]', ' ', text.lower())
        mentioned = False
        if me.username and f"@{me.username.lower()}" in text.lower():
            mentioned = True
        elif "ада" in clean_text.split():
            mentioned = True

        if (
            is_reply_to_me
            or mentioned
            or event.chat_id in _group_batches
            or may_join_group(event.chat_id)
        ):
            should_reply = True
        elif _group_followups.get(event.chat_id, 0) > 0:
            _group_followups[event.chat_id] -= 1
            should_reply = True

    # Пересылка отмечается прямо в тексте, чтобы пометка сохранилась в истории.
    stored_text = text
    if event.fwd_from:
        fwd_name = event.fwd_from.from_name or "Неизвестный источник"
        stored_text = f"[переслано от: {fwd_name}] {text}"

    # Сохраняем сообщение в контексте конкретного чата; автор и цитата — отдельными полями.
    row = {
        "user_id": sender.id, "role": "user", "content": stored_text,
        "timestamp": datetime.now().isoformat(), "sender_name": sender_name,
        "reply_to_text": reply_desc,
    }
    row_id = await db.save_message(
        sender.id, "user", stored_text, chat_id=event.chat_id, sender_name=sender_name,
        tg_message_id=event.id, reply_to_tg_id=reply_msg.id if reply_msg else None,
        reply_to_text=reply_desc,
    )
    item = {"line": format_message_line(row, max_len=4000), "id": row_id, "text": stored_text,
            "forwarded": bool(event.fwd_from)}

    if not should_reply:
        return

    is_muted = await db.get_kv("muted", "false")
    if is_muted == "true" and not is_admin:
        return

    # Модули могут добавить свой контекст к сообщению (хук message_context).
    chat_context += await module_system.message_context(event.message)

    if event.is_private:
        items = await collect_message(event, sender.id, item)
        if items is None:
            return
        if len(items) > 1:
            chat_context += "[Несколько быстрых сообщений собраны в одну законченную мысль.]\n"
    else:
        items = await collect_group_batch(event.chat_id, item)
        if items is None:
            return
        chat_context += "[Ада наблюдала пакет сообщений группы и теперь решает, уместно ли вмешаться.]\n"
    text = "\n".join(entry["text"] for entry in items)
    incoming_lines = "\n".join(entry["line"] for entry in items)
    current_ids = [entry["id"] for entry in items]
    allow_browser = event.is_private and is_admin and not any(entry["forwarded"] for entry in items)

    # Мгновенная отметка о прочтении
    try:
        await event.message.mark_read()
    except Exception:
        pass

    # Индикация набора текста и генерация ответа
    chat_id = event.chat_id
    explicit_memory_request = bool(re.search(
        r"\b(запомни|сохрани в память|запиши в память|запомнить|измени уважение|поменяй уважение|назначь категорию|поменяй категорию|измени категорию|измени настроение|поменяй настроение)\b",
        text.lower(),
    ))
    async with _chat_processing_locks[chat_id]:
        async with client.action(chat_id, 'typing'):
            decision = await brain.process_interaction(
                sender.id,
                incoming_lines,
                is_admin,
                chat_context,
                chat_id=event.chat_id,
                allow_memory_write=explicit_memory_request,
                current_message_ids=current_ids,
                allow_browser=allow_browser,
            )


    if decision.get("error"):
        logger.warning("Ответ в чат %s не отправлен из-за ошибки LLM", chat_id)
        return
    reply_messages = filter_silent_messages(decision.get("messages") or []) if decision.get("must_reply", True) else []
    if not reply_messages:
        logger.info("Ада осознанно промолчала в чате %s", chat_id)
        return
    if not event.is_private:
        _group_last_reply[event.chat_id] = asyncio.get_running_loop().time()
        _group_followups[event.chat_id] = GROUP_FOLLOWUP_MESSAGES
    # Ада отвечает реплаем на сообщение event — его и сохраняем как цитату.
    answered_desc = f"{sender_name}: «{_short(item['text'], 200)}»"
    for reply_text in reply_messages[:6]:
        if not reply_text:
            continue
        await asyncio.sleep(min(2.5, max(0.6, len(reply_text) * 0.02)))
        sent = await send_with_flood_wait(lambda rt=reply_text: event.reply(rt))
        await db.save_message(
            0, "ada", reply_text, chat_id=event.chat_id, sender_name="Ада",
            tg_message_id=getattr(sent, "id", None), reply_to_tg_id=event.id,
            reply_to_text=answered_desc,
        )

@client.on(events.MessageEdited(incoming=True))
async def on_message_edited(event):
    try:
        message = event.message
        if message.text:
            await db.update_message_text(event.chat_id, message.id, message.text)
        await db.add_event(
            "message_edited",
            message.text or "[медиа]",
            chat_id=event.chat_id,
            actor_id=message.sender_id,
            message_id=message.id,
        )
    except Exception:
        logger.exception("Ошибка обработки edited-события")

@client.on(events.MessageDeleted())
async def on_message_deleted(event):
    try:
        await db.add_event(
            "message_deleted",
            json.dumps({"message_ids": event.deleted_ids}, ensure_ascii=False),
            chat_id=event.chat_id,
        )
    except Exception:
        logger.exception("Ошибка обработки deleted-события")

@client.on(events.Raw)
async def on_raw_event(update):
    """Лёгкий журнал реакций без передачи служебного объекта в модель."""
    name = type(update).__name__
    if "Reaction" not in name:
        return
    try:
        await db.add_event("reaction_update", name)
    except Exception:
        logger.exception("Ошибка журналирования реакции")

async def main():
    stop, ready = asyncio.Event(), asyncio.Event()
    async with AsyncExitStack() as stack:
        endpoint = os.environ.pop("ADA_CONTROL_ENDPOINT", "")
        token = os.environ.pop("ADA_CONTROL_SECRET", "")
        if endpoint and token:
            editor = make_editor(db, emotions, personality, modules, agent, tools, stop)
            await stack.enter_async_context(serve(Path(endpoint), token, editor, stop, ready))
        await run_ada(stop, ready)


async def run_ada(stop, ready):
    logger.info("Процесс Ады запущен; инициализация базы данных")
    await db.init()
    logger.info(
        "Проверка LLM: цепочка=%s timeout=%ss",
        " -> ".join(str(p) for p in agent.providers),
        config.LLM_STARTUP_TIMEOUT_SECONDS,
    )
    if not await agent.verify_connection():
        logger.warning("LLM недоступен. Telegram запустится в режиме без генерации до восстановления API.")

    print("[*] Подключение к Telegram...")
    await client.start(phone=config.TG_PHONE)
    asyncio.create_task(outgoing_worker())
    me = await client.get_me()
    saved_profile_name = await db.get_kv("normal_profile_name")
    if not saved_profile_name:
        saved_profile_name = me.first_name or config.NORMAL_NAME
        await db.set_kv("normal_profile_name", saved_profile_name)
    if await db.get_kv("maintenance_restart", "false") == "true":
        await tools.update_my_profile(first_name=saved_profile_name or config.NORMAL_NAME)
        await db.set_kv("maintenance_restart", "false")
        me = await client.get_me()
    logger.info("Ада в сети: %s (@%s)", me.first_name, me.username or "без юзернейма")

    await autonomy.start()
    print("[+] Модуль автономности активирован.")
    await module_system.run_startup_hooks()
    active_modules = modules.active_names()
    print(f"[+] Модули: {', '.join(active_modules) if active_modules else 'нет активных'}")
    if os.name == "nt" and start_overlay():
        print("[+] Локальное меню: Ctrl+Alt+A")
    ready.set()
    stopped = asyncio.create_task(stop.wait())
    disconnected = asyncio.ensure_future(client.run_until_disconnected())
    try:
        done, _ = await asyncio.wait({stopped, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            await disconnected
    finally:
        ready.clear()
        stopped.cancel()
        autonomy.stop()
        try:
            await module_system.run_shutdown_hooks()
            if await db.get_kv("maintenance_restart", "false") != "true":
                await db.set_kv("maintenance_restart", "true")
                if client.is_connected():
                    await tools.update_my_profile(first_name=config.MAINTENANCE_NAME)
        finally:
            await client.disconnect()
            await asyncio.gather(stopped, disconnected, return_exceptions=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Остановка системы.")
