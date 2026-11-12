import sys
import os
import asyncio
import logging
import re
import json
import random
from collections import defaultdict
from telethon import TelegramClient, events, errors
from telethon.tl.types import User, Channel, Chat
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

import config
from database import Database
from emotions import EmotionSystem
from personality import PersonalityManager
from tools import AgentTools
from llm import UniversalAgent
from brain import Brain
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

async def outgoing_worker():
    while True:
        item = await _outgoing_queue.get()
        try:
            await client.send_message(item["chat_id"], item["text"])
            await db.save_message(0, "ada", item["text"], chat_id=item["chat_id"])
        except errors.FloodWaitError as flood:
            logger.warning("FloodWait %ss перед отправкой в %s", flood.seconds, item["chat_id"])
            await asyncio.sleep(flood.seconds + 1)
            try:
                await client.send_message(item["chat_id"], item["text"])
            except Exception as retry_error:
                logger.error("Повторная отправка в %s не удалась: %s", item["chat_id"], retry_error)
        except Exception as e:
            logger.error("Ошибка исходящей отправки в %s: %s", item["chat_id"], e)
        finally:
            _outgoing_queue.task_done()

def enqueue_message(chat_id: int, text: str):
    try:
        _outgoing_queue.put_nowait({"chat_id": chat_id, "text": text[:4096]})
    except asyncio.QueueFull:
        logger.error("Исходящая очередь переполнена; сообщение в %s потеряно", chat_id)

async def send_with_flood_wait(coro_factory):
    """Выполняет отправку, выдерживая FloodWait один раз."""
    try:
        return await coro_factory()
    except errors.FloodWaitError as flood:
        logger.warning("FloodWait %ss; жду и повторяю", flood.seconds)
        await asyncio.sleep(flood.seconds + 1)
        return await coro_factory()

async def collect_message(event, sender_id: int, text: str) -> str | None:
    """Объединяет быстрые сообщения одного автора в одну реплику."""
    key = (event.chat_id, sender_id)
    async with _message_locks[key]:
        current = _pending_messages.get(key)
        if current:
            current["text"] += "\n" + text
            current["task"].cancel()
            # Первое событие остаётся владельцем генерации. Последующие события
            # только расширяют его буфер и не запускают второй ответ.
            is_owner = False
        else:
            future = asyncio.get_running_loop().create_future()
            current = {"text": text, "future": future, "task": None}
            _pending_messages[key] = current
            is_owner = True

        async def flush():
            try:
                await asyncio.sleep(config.MESSAGE_DEBOUNCE_SECONDS)
                item = _pending_messages.pop(key, current)
                owner_future = item["future"]
                if not owner_future.done():
                    owner_future.set_result(item["text"])
            except asyncio.CancelledError:
                return


        current["task"] = asyncio.create_task(flush())
    if not is_owner:
        return None
    return await future

def may_join_group(chat_id: int) -> bool:
    if not config.GROUP_AUTONOMY_ENABLED:
        return False
    now = asyncio.get_running_loop().time()
    last = _group_last_reply.get(chat_id, 0.0)
    if now - last < config.GROUP_REPLY_COOLDOWN_MINUTES * 60:
        return False
    return random.random() < config.GROUP_REPLY_PROBABILITY

async def collect_group_batch(chat_id: int, text: str) -> str | None:
    """После первого сигнала собирает первое и следующие пять сообщений группы."""
    async with _group_batch_locks[chat_id]:
        batch = _group_batches.get(chat_id)
        if batch:
            batch["texts"].append(text)
            if len(batch["texts"]) >= 6:
                if not batch["future"].done():
                    batch["future"].set_result("\n".join(batch["texts"]))
                _group_batches.pop(chat_id, None)
            return None

        future = asyncio.get_running_loop().create_future()
        batch = {"texts": [text], "future": future}
        _group_batches[chat_id] = batch

        async def finish_later():
            await asyncio.sleep(max(4.0, config.MESSAGE_DEBOUNCE_SECONDS + 2.0))
            if not future.done():
                future.set_result("\n".join(batch["texts"]))
            _group_batches.pop(chat_id, None)

        asyncio.create_task(finish_later())
    return await future

async def check_admin(sender: User) -> bool:
    saved_admin_id = await db.get_kv("admin_id")
    configured_admin_id = config.ADMIN_USER_ID
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
    if command == "расписание" and len(parts) >= 2 and parts[1].lower() == "импорт":
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

    if event.is_private:
        should_reply = True
        if is_new_user:
            chat_context = (
                "[Это первое личное обращение. Секретарское приветствие уже отправлено отдельным сообщением; не повторяй его. "
                "Ответь на конкретную просьбу человека, если она есть.]\n"
            )
    else:
        chat = await event.get_chat()
        chat_title = getattr(chat, 'title', 'Группа')
        await db.upsert_chat(event.chat_id, chat_title, type(chat).__name__)
        chat_context = f"[Сообщение из группового чата: '{chat_title}']\n"

        is_reply_to_me = False
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.sender_id == me.id:
                is_reply_to_me = True
            elif reply_msg:
                # Фиксируем контекст ответа другому человеку
                rep_sender = await reply_msg.get_sender()
                rep_name = getattr(rep_sender, 'first_name', 'Кто-то')
                chat_context += f"[Ответ на реплику {rep_name}: '{reply_msg.text}']\n"

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

    # Проверка на пересылку
    if event.fwd_from:
        fwd_name = event.fwd_from.from_name or "Неизвестный источник"
        chat_context += f"[Переслано от: {fwd_name}]\n"

    # Сохраняем сообщение в контексте конкретного чата, а не только автора.
    sender_name = " ".join(filter(None, [sender.first_name, sender.last_name])) or sender.username or str(sender.id)
    full_log_text = f"[Автор: {sender_name}, ID: {sender.id}]\n{chat_context}{text}".strip()
    await db.save_message(sender.id, "user", full_log_text, chat_id=event.chat_id)

    if not should_reply:
        return

    is_muted = await db.get_kv("muted", "false")
    if is_muted == "true" and not is_admin:
        return

    if event.is_private:
        combined_text = await collect_message(event, sender.id, text)
        if combined_text is None:
            return
        if combined_text != text:
            text = combined_text
            chat_context += "[Несколько быстрых сообщений собраны в одну законченную мысль.]\n"
    else:
        combined_text = await collect_group_batch(event.chat_id, f"{sender_name}: {text}")
        if combined_text is None:
            return
        text = combined_text
        chat_context += "[Ада наблюдала пакет сообщений группы и теперь решает, уместно ли вмешаться.]\n"
        await db.update_chat_context(event.chat_id, summary=text)

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
                text,
                is_admin,
                chat_context,
                chat_id=event.chat_id,
                allow_memory_write=explicit_memory_request,
            )


    reply_messages = decision.get("messages") or (
        [decision.get("reply")] if decision.get("reply") else []
    )
    if reply_messages:
        if not event.is_private:
            _group_last_reply[event.chat_id] = asyncio.get_running_loop().time()
        for reply_text in reply_messages[:6]:
            if not reply_text:
                continue
            await asyncio.sleep(min(2.5, max(0.6, len(reply_text) * 0.02)))
            await send_with_flood_wait(lambda rt=reply_text: event.reply(rt))
            await db.save_message(0, "ada", reply_text, chat_id=event.chat_id)

@client.on(events.MessageEdited(incoming=True))
async def on_message_edited(event):
    try:
        message = event.message
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
