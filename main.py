import sys
import os
import asyncio
import logging
import re
import json
import random
from collections import defaultdict
from datetime import datetime
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
import vision
from autonomy import AutonomyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOGS_DIR / "ada.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("Ada.Main")

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
agent = UniversalAgent(tools)
brain = Brain(db, emotions, personality, agent)

async def send_user_message(target_id: int, text: str):
    enqueue_message(target_id, text)

autonomy = AutonomyEngine(db, brain, emotions, send_user_message)
_instance_lock_file = None

_pending_messages: dict[tuple[int, int], dict] = {}

_message_locks = defaultdict(asyncio.Lock)
_group_last_reply: dict[int, float] = {}
_group_batches: dict[int, dict] = {}
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
    enqueue_message(
        admin_id,
        f"⚠️ Ошибка LLM API ({config.LLM_PROVIDER}/{config.ACTIVE_LLM_MODEL}).\n"
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

def start_overlay():
    loop = asyncio.get_running_loop()
    import overlay
    if config.VOICE_ENABLED:
        import whisper_transcribe
        whisper_transcribe.preload()

    def on_submit(text: str):
        future = asyncio.run_coroutine_threadsafe(handle_local_request(text), loop)

        def done(f):
            if f.exception():
                logger.error("Локальный запрос упал: %s", f.exception())
                overlay.show_reply(f"Что-то сломалось: {f.exception()}")
            else:
                overlay.show_reply(f.result())
        future.add_done_callback(done)

    def transcribe(wav: bytes) -> str:
        future = asyncio.run_coroutine_threadsafe(whisper_transcribe.transcribe(wav), loop)
        return future.result(timeout=90)

    overlay.start(on_submit, transcribe if config.VOICE_ENABLED else None)

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
    label = await vision.media_label(reply_msg) if config.VISION_ENABLED else ""
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

async def handle_admin_command(event, command: str, args: list) -> bool:
    cmd = command.lower()

    if cmd == "/status":
        state = await emotions.get_state()
        traits = personality.get_traits()
        auto = await db.get_kv("autonomy_enabled", "true" if config.AUTONOMY_ENABLED else "false")
        
        emo_lines = "\n".join([f"• {k}: {v}" for k, v in state.items()])
        trait_lines = "\n".join([f"• {k}: {v}" for k, v in traits.items()])
        
        text = (
            f"🧠 **Состояние Ады (Gemini ReAct + Shell + Search):**\n\n"
            f"**Эмоции:**\n{emo_lines}\n\n"
            f"**Черты:**\n{trait_lines}\n\n"
            f"**Автономность:** {'ВКЛ' if auto == 'true' else 'ВЫКЛ'}"
        )
        await event.reply(text)
        return True

    elif cmd == "/set_mood" and len(args) >= 2:
        try:
            await emotions.set_emotion(args[0].lower(), int(args[1]))
            await event.reply(f"Настроение обновлено: `{args[0]}` = {args[1]}")
        except ValueError:
            pass
        return True

    elif cmd == "/set_trait" and len(args) >= 2:
        try:
            if personality.set_trait(args[0].lower(), int(args[1])):
                await event.reply(f"Черта обновлена: `{args[0]}` = {args[1]}")
        except ValueError:
            pass
        return True

    elif cmd == "/autonomy" and len(args) >= 1:
        state = "true" if args[0].lower() == "on" else "false"
        await db.set_kv("autonomy_enabled", state)
        await event.reply(f"Автономность: {'ВКЛ' if state == 'true' else 'ВЫКЛ'}")
        return True

    elif cmd == "/memory":
        mems = await db.get_all_memories()
        if not mems:
            await event.reply("В долгосрочной памяти пока пусто.")
        else:
            lines = [f"- [{m['category']}] {m['content']}" for m in mems[-8:]]
            await event.reply("**Воспоминания:**\n" + "\n".join(lines))
        return True

    return False


EDIT_CODE = "8338"

async def edit_menu(event) -> None:
    await event.reply(
        "🛠 Режим редактирования включён. ИИ не вызывается.\n\n"
        "Команды:\n"
        "• эмоции — показать эмоции\n"
        "• эмоции <название> <0..100> — изменить эмоцию\n"
        "• состояние — показать аккаунт и текущие настройки\n"
        "• память — показать долговременную память\n"
        "• память добавить личное|группа|global <текст> — добавить точный факт\n"

        "• память удалить <id> — удалить факт\n"
        "• память история — журнал изменений фактов\n"
        "• память очистить — очистить историю и память\n"
        "• правило <chat_id> <текст> — локальное правило группы\n"
        "• уважение <id> <0..100> — изменить уважение\n"
        "• категория <id> стандарт|друг|админ — изменить категорию\n"
        "• роль добавить <id> стандарт|друг — назначить роль (админа только владелец в конфиге)\n"
        "• календарь добавить <ISO-дата-время> <название> — добавить событие\n"
        "• календарь — список событий\n"
        "• календарь добавить <ISO-дата-время> <название> — добавить встречу\n"
        "• календарь удалить <id> — удалить встречу\n"
        "• расписание импорт <JSON> — загрузить расписание\n"
        "• расписание показать — посмотреть загруженное расписание\n"
        "• расписание сегодня|завтра — расписание УКРТБ с сайта\n"
        "• расписание обновить — обновить кэш расписания УКРТБ\n"
        "• задача отменить <id> — отменить напоминание\n"
        "• задачи — список напоминаний\n"
        "• запросы — запросы на встречи\n"
        "• техработы — включить служебное имя и остановить\n"
        "• перезапуск — перезапустить процесс через 2 секунды\n"
        "• промпт — показать системный промпт\n"

        "• промпт установить <текст> — заменить базовый промпт\n"
        "• имя <текст> — изменить имя Telegram-профиля\n"
        "• био <текст> — изменить био Telegram-профиля\n"
        "• выйти — вернуться в обычный режим"
    )

async def set_profile_name(first_name: str):
    await tools.update_my_profile(first_name=first_name)

async def restart_process():
    await client.disconnect()
    await asyncio.sleep(2)
    os.execl(sys.executable, sys.executable, *sys.argv)

async def handle_edit_mode(event, text: str) -> bool:
    tools.set_request_context(event.sender_id or config.ADMIN_USER_ID, event.chat_id or 0, True, True)
    parts = text.split(maxsplit=3)
    command = parts[0].lower() if parts else ""

    if command == "выйти":
        await db.set_kv("edit_mode", "false")
        await event.reply("Обычный режим восстановлен.")
        return True
    if command in {"помощь", "меню", "?"}:
        await edit_menu(event)
        return True
    if command == "эмоции":
        if len(parts) == 1:
            await event.reply(json.dumps(await emotions.get_state(), ensure_ascii=False, indent=2))
        elif len(parts) == 3:
            try:
                await emotions.set_emotion(parts[1].lower(), int(parts[2]))
                await event.reply(f"Эмоция изменена: {parts[1]} = {parts[2]}")
            except (ValueError, TypeError):
                await event.reply("Формат: эмоции energy 80")
        else:
            await event.reply("Формат: эмоции <название> <0..100>")
        return True
    if command == "состояние":
        await event.reply(
            f"Провайдер: {config.LLM_PROVIDER}\nМодель: {config.ACTIVE_LLM_MODEL}\n"
            f"Автономность: {await db.get_kv('autonomy_enabled', 'true')}\n"
            f"Эмоции: {json.dumps(await emotions.get_state(), ensure_ascii=False)}"
        )
        return True
    if command == "перезапуск":
        await event.reply("Перезапускаюсь через 2 секунды.")
        asyncio.create_task(restart_process())
        return True
    if command == "техработы":
        await db.set_kv("maintenance_restart", "true")
        await tools.update_my_profile(first_name=config.MAINTENANCE_NAME)
        await event.reply("Включаю режим технических работ и останавливаюсь.")
        client.disconnect()
        return True
    if command == "уважение" and len(parts) == 3:
        try:
            await db.update_user_relationship(int(parts[1]), {"respect": max(0, min(100, int(parts[2])))})
            await event.reply("Уровень уважения обновлён.")
        except ValueError:
            await event.reply("Формат: уважение <id> <0..100>")
        return True
    if command == "категория" and len(parts) == 3:
        category = parts[2].lower()
        target_id = int(parts[1])
        if category in {"стандарт", "друг", "админ"}:
            await db.set_user_category(target_id, category)
            await event.reply(f"Категория пользователя изменена: {category}.")
        else:
            await event.reply("Категория: стандарт, друг или админ.")
        return True
    if command == "роль" and len(parts) == 4 and parts[1].lower() == "добавить":
        category = parts[3].lower()
        if category not in {"стандарт", "друг"}:
            await event.reply("Команда может назначить только стандарт или друг.")
        else:
            await db.set_user_category(int(parts[2]), category)
            await event.reply(f"Категория назначена: {category}.")
        return True
    if command == "эмоция" and len(parts) == 3:
        try:
            await emotions.set_emotion(parts[1].lower(), int(parts[2]))
            await event.reply("Эмоциональное состояние обновлено.")
        except ValueError:
            await event.reply("Формат: эмоция happiness 70")
        return True
    if command == "календарь":
        if len(parts) == 1:
            await event.reply(await tools.list_calendar_events())
        elif len(parts) >= 4 and parts[1].lower() == "добавить":
            title = parts[3]
            await event.reply(await tools.add_calendar_event(config.ADMIN_USER_ID, title, parts[2]))
        elif len(parts) == 3 and parts[1].lower() == "удалить":
            try:
                await event.reply(await tools.delete_calendar_event(int(parts[2])))
            except ValueError:
                await event.reply("Формат: календарь удалить <id>")
        else:
            await event.reply("Формат: календарь добавить <ISO-дата-время> <название>")
        return True
    if command == "расписание" and len(parts) >= 2 and parts[1].lower() == "показать":
        await event.reply(await tools.read_schedule())
        return True
    if command == "расписание" and len(parts) >= 3 and parts[1].lower() == "импорт":
        await event.reply(await tools.import_schedule_json(parts[2]))
        return True
    if command == "расписание" and len(parts) >= 2 and parts[1].lower() == "обновить":
        await event.reply(await tools.refresh_my_schedule_cache())
        return True
    if command == "расписание" and len(parts) >= 2 and parts[1].lower() in {"сегодня", "завтра", "укртб"}:
        when = "сегодня" if parts[1].lower() == "сегодня" else ("завтра" if parts[1].lower() == "завтра" else "")
        await event.reply(await tools.get_college_schedule(when))
        return True
    if command == "задача" and len(parts) == 3 and parts[1].lower() == "отменить":
        try:
            await event.reply(await tools.cancel_task(int(parts[2])))
        except ValueError:
            await event.reply("Формат: задача отменить <id>")
        return True
    if command == "задачи":
        await event.reply(await tools.list_tasks(config.ADMIN_USER_ID))
        return True
    if command == "запросы":
        await event.reply(await tools.list_meeting_requests())
        return True
    if command == "правило":
        if len(parts) >= 3 and parts[1].lstrip("-").isdigit():
            await db.update_chat_context(int(parts[1]), rules=parts[2])
            await event.reply(f"Правило чата {parts[1]} сохранено.")
        elif len(parts) == 3 and parts[1].lower() == "показать" and parts[2].lstrip("-").isdigit():
            chat_row = await db.get_chat(int(parts[2]))
            await event.reply(f"Правила чата {parts[2]}:\n{(chat_row or {}).get('rules') or 'не заданы'}")
        else:
            await event.reply("Формат: правило <chat_id> <текст правила>")
        return True
    if command == "память" and len(parts) == 2 and parts[1].lower() == "история":
        history = await db.get_memory_history(limit=25)
        text_out = "\n".join(
            f"#{h['memory_id']} [{h['action']}] {h['content'][:120]}" for h in history
        )
        await event.reply(text_out[:3900] or "История памяти пуста.")
        return True
    if command == "память" and len(parts) == 2 and parts[1].lower() == "очистить":
        await db.clear_memory()
        await event.reply("Память, история и журналы очищены.")
        return True
    if command == "память":
        if len(parts) == 1:
            memories = await db.list_scoped_memories(60)
            text_out = "\n".join(
                f"#{m['id']} [{m['scope']}/{m['category']}; {m['confidence']:.2f}] {m['content']}"
                for m in memories
            )
            await event.reply(text_out[:3900] or "Память пуста.")

        elif len(parts) == 4 and parts[1].lower() == "добавить":
            scope = parts[2].lower()
            if scope not in {"личное", "группа", "global"}:
                await event.reply("Область памяти: личное, группа или global.")
            else:
                await db.add_scoped_memory(
                    {"личное": "person", "группа": "group", "global": "global"}[scope],
                    parts[3],
                    category="explicit_fact",
                    owner_id=config.ADMIN_USER_ID if scope == "личное" else None,
                    chat_id=event.chat_id if scope == "группа" else None,
                    importance=8,
                    confidence=1.0,
                    source="owner_edit_mode",
                )
                await event.reply("Подтверждённый факт добавлен.")
        elif len(parts) == 3 and parts[1].lower() == "удалить":
            try:
                await db.delete_memory(int(parts[2]))
                await event.reply("Факт удалён.")

            except ValueError:
                await event.reply("Формат: память удалить <id>")
        else:
            await event.reply("Формат: память добавить <категория> <текст>")
        return True
    if command == "промпт":
        if len(parts) == 1:
            await event.reply(personality.get_prompt()[:3900])
        elif len(parts) >= 3 and parts[1].lower() == "установить":
            personality.set_prompt(parts[2])
            await event.reply("Базовый промпт обновлён.")
        return True
    if command in {"имя", "био"} and len(parts) >= 2:
        value = text.partition(" ")[2].strip()
        result = await tools.update_my_profile(first_name=value if command == "имя" else "", about=value if command == "био" else "")
        await event.reply(result)
        return True
    await event.reply("Неизвестная команда. Напишите «меню» или «выйти».")
    return True

@client.on(events.NewMessage(incoming=True))
async def on_incoming_message(event):
    sender = await event.get_sender()
    
    if not isinstance(sender, User) or sender.bot or sender.is_self:
        return

    text = event.raw_text.strip()
    if event.message.voice or event.message.audio or event.message.video_note:
        await event.reply("Я не могу прослушивать голосовые и звонки. Напиши это текстом — так будет удобнее.")
        return
    if config.VISION_ENABLED and (event.is_private or event.message.mentioned):
        label = await vision.media_label(event.message)
        text = f"{label} {text}".strip() if label else text
    if not text:
        return

    me = await client.get_me()
    is_admin = await check_admin(sender)

    if is_admin and event.is_private and text.lower() == f"редактирование {EDIT_CODE}":
        await db.set_kv("edit_mode", "true")
        await edit_menu(event)
        return

    if is_admin and event.is_private and await db.get_kv("edit_mode", "false") == "true":
        await handle_edit_mode(event, text)
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

    # Обработка команд админа
    if is_admin and text.startswith("/"):
        parts = text.split()
        if await handle_admin_command(event, parts[0], parts[1:]):
            return

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
    item = {"line": format_message_line(row, max_len=4000), "id": row_id, "text": stored_text}

    if not should_reply:
        return

    is_muted = await db.get_kv("muted", "false")
    if is_muted == "true" and not is_admin:
        return

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

def acquire_instance_lock():
    global _instance_lock_file
    lock_path = config.DATA_DIR / "ada.lock"
    _instance_lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        if _instance_lock_file.tell() == 0:
            _instance_lock_file.write("0")
            _instance_lock_file.flush()
        if os.name == "nt":
            import msvcrt
            _instance_lock_file.seek(0)
            msvcrt.locking(_instance_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_instance_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        logger.error("Ада уже запущена: не удалось получить lock %s", lock_path)
        raise SystemExit(2)

async def main():
    acquire_instance_lock()
    logger.info("Процесс Ады запущен; инициализация базы данных")
    await db.init()
    logger.info(
        "Проверка LLM: provider=%s model=%s timeout=%ss",
        config.LLM_PROVIDER,
        config.ACTIVE_LLM_MODEL,
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
    print(f"[+] Ада в сети: {me.first_name} (@{me.username or 'без юзернейма'})")

    await autonomy.start()
    print("[+] Модуль автономности активирован.")
    if os.name == "nt":
        start_overlay()
        print("[+] Локальное меню: Win+Shift+A")
    try:
        await client.run_until_disconnected()
    finally:
        autonomy.stop()
        if await db.get_kv("maintenance_restart", "false") != "true":
            await db.set_kv("maintenance_restart", "true")
            try:
                await tools.update_my_profile(first_name=config.MAINTENANCE_NAME)
            except Exception:
                logger.exception("Не удалось установить служебное имя")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Остановка системы.")
