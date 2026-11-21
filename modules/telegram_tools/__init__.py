"""Модуль инструментов Telegram: диалоги, история, поиск, редактирование/удаление/
реакции/пересылка/закрепление сообщений, участники и информация о чате.

Включение: MODULE_TELEGRAM_TOOLS=true в .env.

send_message_to_user, send_file и update_my_profile остаются в ядре (tools.py):
main.py вызывает update_my_profile напрямую при техработах и перезапуске, а
send_file нужен ядру для ответов с файлами в разных сценариях. Здесь — всё,
что можно включать и выключать независимо.
"""

import logging

from telethon.tl.functions.messages import DeleteMessagesRequest, EditMessageRequest, SendReactionRequest
from telethon.tl.types import ReactionEmoji, User

from module_system import ModuleContext

DESCRIPTION = "Telegram: диалоги, история, поиск, редактирование и реакции на сообщения"
VERSION = "1.0"
DEFAULT_ENABLED = True

logger = logging.getLogger("Ada.Module.telegram_tools")


def _entity_ref(target: str):
    return int(target) if str(target).lstrip("-").isdigit() else target


def setup(ctx: ModuleContext) -> None:
    client = ctx.client
    db = ctx.db

    async def list_dialogs(limit: int = 30) -> str:
        """Возвращает список последних диалогов аккаунта."""
        try:
            dialogs = await client.get_dialogs(limit=max(1, min(int(limit), 100)))
            lines = []
            for dialog in dialogs:
                entity = dialog.entity
                title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Без названия"
                lines.append(f"ID={dialog.id} | {title} | непрочитано={dialog.unread_count}")
            return "\n".join(lines) or "Диалоги не найдены."
        except Exception as exc:
            return f"Ошибка получения диалогов: {exc}"

    async def read_chat_history(target: str, limit: int = 20) -> str:
        """Читает последние сообщения указанного чата."""
        try:
            entity = await client.get_entity(_entity_ref(target))
            messages = await client.get_messages(entity, limit=max(1, min(int(limit), 100)))
            lines = []
            for message in reversed(messages):
                sender = await message.get_sender()
                name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Неизвестно"
                lines.append(f"[{message.id}] {name}: {message.text or '[медиа]'}")
            return "\n".join(lines) or "История пуста."
        except Exception as exc:
            return f"Ошибка чтения истории: {exc}"

    async def search_telegram(query: str, limit: int = 20) -> str:
        """Ищет сообщения по всему Telegram, доступные аккаунту."""
        try:
            messages = await client.get_messages(None, search=query, limit=max(1, min(int(limit), 100)))
            lines = [f"[{m.id}] chat={getattr(m, 'peer_id', '')}: {m.text or '[медиа]'}" for m in messages]
            return "\n".join(lines) or "Совпадений нет."
        except Exception as exc:
            return f"Ошибка поиска: {exc}"

    async def find_contact(query: str) -> str:
        """Ищет пользователя по username или имени в Telegram и локальной базе."""
        try:
            entity = await client.get_entity(query)
            if isinstance(entity, User):
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                return f"Telegram пользователь: ID={entity.id}, @{entity.username}, Имя: {name}"
        except Exception:
            pass

        import aiosqlite
        async with aiosqlite.connect(db.db_path) as conn:
            cursor = await conn.execute(
                "SELECT user_id, username, first_name, status, respect FROM users WHERE username LIKE ? OR first_name LIKE ?",
                (f"%{query}%", f"%{query}%"),
            )
            rows = await cursor.fetchall()
            if rows:
                items = [f"ID={r[0]}, @{r[1]}, Имя: {r[2]}, Статус: {r[3]}, Respect: {r[4]}" for r in rows]
                return "Найдено в базе данных:\n" + "\n".join(items)

        return f"Контакт '{query}' не найден."

    async def edit_message(target: str, message_id: int, new_text: str) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            await client(EditMessageRequest(peer=entity, id=int(message_id), message=new_text[:4096]))
            return f"Сообщение {message_id} отредактировано."
        except Exception as exc:
            return f"Не удалось отредактировать сообщение: {exc}"

    async def delete_message(target: str, message_id: int, revoke: bool = True) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            await client.delete_messages(entity, [int(message_id)], revoke=revoke)
            return f"Сообщение {message_id} удалено."
        except Exception as exc:
            return f"Не удалось удалить сообщение: {exc}"

    async def reply_to_message(target: str, message_id: int, message: str) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            sent = await ctx.tools._send(lambda: client.send_message(entity, message[:4096], reply_to=int(message_id)))
            from telethon.utils import get_peer_id
            await db.save_message(0, "ada", message[:4096], chat_id=get_peer_id(entity),
                                  sender_name="Ада", tg_message_id=getattr(sent, "id", None),
                                  reply_to_tg_id=int(message_id), reply_to_text=f"сообщение #{message_id}")
            return f"Ответ на сообщение {message_id} отправлен."
        except Exception as exc:
            return f"Не удалось ответить на сообщение: {exc}"

    async def react_to_message(target: str, message_id: int, reaction: str = "👍") -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            await client(SendReactionRequest(
                peer=entity, msg_id=int(message_id),
                reaction=[ReactionEmoji(emoticon=str(reaction)[:8])],
            ))
            return f"Реакция {reaction} добавлена."
        except Exception as exc:
            return f"Не удалось поставить реакцию: {exc}"

    async def forward_message(target: str, message_id: int, destination: str) -> str:
        try:
            source = await client.get_entity(_entity_ref(target))
            dest = await client.get_entity(_entity_ref(destination))
            await client.forward_messages(dest, int(message_id), source)
            return "Сообщение переслано."
        except Exception as exc:
            return f"Не удалось переслать сообщение: {exc}"

    async def pin_message(target: str, message_id: int, notify: bool = False) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            await client.pin_message(entity, int(message_id), notify=notify)
            return "Сообщение закреплено."
        except Exception as exc:
            return f"Не удалось закрепить сообщение: {exc}"

    async def mark_chat_read(target: str) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            await client.send_read_acknowledge(entity)
            return "Чат отмечен прочитанным."
        except Exception as exc:
            return f"Не удалось отметить чат прочитанным: {exc}"

    async def get_group_members(target: str, limit: int = 50) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            users = await client.get_participants(entity, limit=max(1, min(int(limit), 200)))
            return "\n".join(
                f"ID={u.id} @{u.username or '-'} {u.first_name or ''} {u.last_name or ''}".strip()
                for u in users
            ) or "Участники не найдены."
        except Exception as exc:
            return f"Не удалось получить участников: {exc}"

    async def get_chat_info(target: str) -> str:
        try:
            entity = await client.get_entity(_entity_ref(target))
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or ""
            return f"ID={entity.id}\nНазвание: {title}\nТип: {type(entity).__name__}"
        except Exception as exc:
            return f"Не удалось получить информацию о чате: {exc}"

    target_id = {"target": {"type": "string"}, "message_id": {"type": "integer"}}
    ctx.register_tool("list_dialogs", list_dialogs, "Получить список последних диалогов Telegram.",
                      {"limit": {"type": "integer"}}, [])
    ctx.register_tool("read_chat_history", read_chat_history, "Прочитать последние сообщения личного чата или группы.",
                      {"target": {"type": "string"}, "limit": {"type": "integer"}}, ["target"])
    ctx.register_tool("search_telegram", search_telegram, "Найти сообщения в доступной истории Telegram.",
                      {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])
    ctx.register_tool("find_contact", find_contact, "Найти человека в Telegram или базе данных.",
                      {"query": {"type": "string"}}, ["query"])
    ctx.register_tool("edit_message", edit_message, "Отредактировать своё сообщение.",
                      {**target_id, "new_text": {"type": "string"}}, ["target", "message_id", "new_text"])
    ctx.register_tool("delete_message", delete_message, "Удалить сообщение.",
                      {**target_id, "revoke": {"type": "boolean"}}, ["target", "message_id"])
    ctx.register_tool("reply_to_message", reply_to_message, "Ответить на конкретное сообщение.",
                      {**target_id, "message": {"type": "string"}}, ["target", "message_id", "message"])
    ctx.register_tool("react_to_message", react_to_message, "Поставить реакцию.",
                      {**target_id, "reaction": {"type": "string"}}, ["target", "message_id"])
    ctx.register_tool("forward_message", forward_message, "Переслать сообщение.",
                      {**target_id, "destination": {"type": "string"}}, ["target", "message_id", "destination"])
    ctx.register_tool("pin_message", pin_message, "Закрепить сообщение.",
                      {**target_id, "notify": {"type": "boolean"}}, ["target", "message_id"])
    ctx.register_tool("mark_chat_read", mark_chat_read, "Отметить чат прочитанным.",
                      {"target": {"type": "string"}}, ["target"])
    ctx.register_tool("get_group_members", get_group_members, "Получить участников группы.",
                      {"target": {"type": "string"}, "limit": {"type": "integer"}}, ["target"])
    ctx.register_tool("get_chat_info", get_chat_info, "Получить информацию о чате.",
                      {"target": {"type": "string"}}, ["target"])
    ctx.register_capability(
        "list_dialogs / read_chat_history / search_telegram / find_contact / edit_message / delete_message / "
        "reply_to_message / react_to_message / forward_message / pin_message / mark_chat_read / "
        "get_group_members / get_chat_info: управление сообщениями и диалогами Telegram."
    )
