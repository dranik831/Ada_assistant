import sys
import asyncio
import logging
import re
import json
import random
from collections import defaultdict
from telethon import TelegramClient, events
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

tools = AgentTools(db, client)
agent = UniversalAgent(tools)
brain = Brain(db, emotions, personality, agent)

async def send_user_message(target_id: int, text: str):
    try:
        async with client.action(target_id, 'typing'):
            await asyncio.sleep(min(3.0, max(1.0, len(text) * 0.02)))
        await client.send_message(target_id, text)
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение {target_id}: {e}")

autonomy = AutonomyEngine(db, brain, emotions, send_user_message)

_pending_messages: dict[tuple[int, int], dict] = {}
_message_locks = defaultdict(asyncio.Lock)
_group_last_reply: dict[int, float] = {}
_group_batches: dict[int, dict] = {}
_group_batch_locks = defaultdict(asyncio.Lock)

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
                if not future.done():
                    future.set_result(item["text"])
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
        "• память добавить <категория> <текст> — добавить факт\n"
        "• память удалить <id> — удалить факт\n"
        "• промпт — показать системный промпт\n"
        "• промпт установить <текст> — заменить базовый промпт\n"
        "• имя <текст> — изменить имя Telegram-профиля\n"
        "• био <текст> — изменить био Telegram-профиля\n"
        "• выйти — вернуться в обычный режим"
    )

async def handle_edit_mode(event, text: str) -> bool:
    parts = text.split(maxsplit=2)
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
    if command == "память":
        if len(parts) == 1:
            memories = await db.get_all_memories()
            text_out = "\n".join(f"#{m['id']} [{m['category']}] {m['content']}" for m in memories[-20:])
            await event.reply(text_out or "Память пуста.")
        elif len(parts) >= 3 and parts[1].lower() == "добавить":
            category, content = parts[2].split(maxsplit=1) if " " in parts[2] else ("general", parts[2])
            await db.add_memory(category, content, importance=8)
            await event.reply("Факт добавлен в память.")
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

    # Регистрация пользователя в локальной БД
    await db.get_or_create_user(sender.id, sender.username or "", sender.first_name or "", is_admin=is_admin)

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
    async with client.action(chat_id, 'typing'):
        decision = await brain.process_interaction(sender.id, text, is_admin, chat_context, chat_id=event.chat_id)

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
            await event.reply(reply_text)
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

async def main():
    await db.init()
    
    if not await agent.verify_connection():
        logger.error("Проверьте GEMINI_API_KEY в .env!")
        return

    print("[*] Подключение к Telegram...")
    await client.start(phone=config.TG_PHONE)
    me = await client.get_me()
    print(f"[+] Ада в сети: {me.first_name} (@{me.username or 'без юзернейма'})")

    await autonomy.start()
    print("[+] Модуль автономности активирован.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Остановка системы.")
