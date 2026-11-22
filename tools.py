import asyncio
import logging
from contextvars import ContextVar
from pathlib import Path
import aiohttp
import trafilatura
from telethon import TelegramClient, errors
from telethon.utils import get_peer_id
from telethon.tl.functions.account import UpdateProfileRequest
from database import Database
import config
import module_system

logger = logging.getLogger("Ada.Tools")

class AgentTools:
    def __init__(self, db: Database, tg_client: TelegramClient, emotions=None):
        self.db = db
        self.client = tg_client
        self.emotions = emotions
        self._admin_context: ContextVar[bool] = ContextVar("ada_admin_context", default=False)
        self._actor_context: ContextVar[int] = ContextVar("ada_actor_context", default=0)
        self._chat_context: ContextVar[int] = ContextVar("ada_chat_context", default=0)
        self._memory_write_context: ContextVar[bool] = ContextVar("ada_memory_write_context", default=False)
        self._calendar_lock = asyncio.Lock()


    def set_admin_context(self, is_admin: bool):
        self._admin_context.set(bool(is_admin))

    def set_request_context(self, actor_id: int, chat_id: int, is_admin: bool, allow_memory_write: bool = False):
        self._actor_context.set(actor_id)
        self._chat_context.set(chat_id)
        self._admin_context.set(bool(is_admin))
        self._memory_write_context.set(bool(allow_memory_write))

    def _is_admin(self) -> bool:

        return self._admin_context.get()

    @staticmethod
    async def _send(coro_factory):
        """Отправка с одним повтором после FloodWait (как send_with_flood_wait в main.py)."""
        try:
            return await coro_factory()
        except errors.FloodWaitError as flood:
            logger.warning("FloodWait %ss; жду и повторяю", flood.seconds)
            await asyncio.sleep(flood.seconds + 1)
            return await coro_factory()

    def _pc_allowed(self) -> bool:
        # Управление ПК: только Создатель и только в его личке/локальном меню.
        # В группе пакет сообщений идёт от имени первого автора, туда могут попасть чужие реплики.
        return self._is_admin() and self._chat_context.get() > 0

    async def list_my_capabilities(self) -> str:
        """Возвращает перечень доступных возможностей Ады, включая подключённые модули."""
        base = (
            "Инструменты, доступные прямо сейчас:\n"
            "1. fetch_web_page: чтение статей по ссылкам в интернете.\n"
            "2. send_message_to_user / send_file: отправка сообщений и файлов пользователям и группам.\n"
            "3. search_memory / search_scoped_memory: долговременная память.\n"
            "4. update_my_profile: изменение имени и описания аккаунта.\n"
            "5. set_person_category / set_person_respect / set_mood: управление отношениями и настроением.\n"
            "6. Инструменты Telegram (диалоги, история, реакции, редактирование и т.д.) — доступны напрямую."
        )
        modules = module_system.registry().loadable_tool_modules()
        other_modules = [name for name in modules if name != "telegram_tools"]
        if other_modules:
            base += (
                "\n\nДругие подключённые модули (их инструменты сначала нужно узнать через "
                f"list_module_tools, указав module_name): {', '.join(other_modules)}."
            )
        return base

    async def fetch_web_page(self, url: str) -> str:
        """Загружает страницу из интернета и извлекает чистый текст статьи."""
        logger.info(f"Чтение веб-страницы: {url}")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=12) as resp:
                    if resp.status != 200:
                        return f"Ошибка загрузки страницы: HTTP {resp.status}"
                    html = await resp.text()

            text = trafilatura.extract(html, include_links=False, include_images=False)
            if not text:
                return "Не удалось извлечь структурированный текст со страницы."
            return text[:3500]
        except Exception as e:
            return f"Ошибка загрузки URL: {e}"

    async def update_my_profile(self, first_name: str = "", about: str = "") -> str:
        """Изменяет имя и статус (био) Telegram-аккаунта Ады."""
        try:
            kwargs = {}
            if first_name: kwargs['first_name'] = first_name
            if about: kwargs['about'] = about
            if not kwargs:
                return "Не указаны параметры для обновления."
            await self.client(UpdateProfileRequest(**kwargs))
            return f"Профиль обновлен: {kwargs}"
        except Exception as e:
            return f"Не удалось обновить профиль: {e}"

    async def send_message_to_user(self, target: str, message: str) -> str:
        """Отправляет сообщение пользователю или в группу по ID или юзернейму."""
        try:
            clean_target = str(target).strip()
            if clean_target.startswith("-") and clean_target[1:].isdigit():
                entity_target = int(clean_target)
            elif clean_target.isdigit():
                entity_target = int(clean_target)
            else:
                entity_target = clean_target

            try:
                entity = await self.client.get_entity(entity_target)
            except Exception:
                await self.client.get_dialogs(limit=50)
                entity = await self.client.get_entity(entity_target)

            message = message[:4096]
            sent = await self._send(lambda: self.client.send_message(entity, message))
            await self.db.save_message(0, "ada", message, chat_id=get_peer_id(entity),
                                       sender_name="Ада", tg_message_id=getattr(sent, "id", None))
            return f"Сообщение успешно отправлено в {target}."
        except Exception as e:
            return f"Ошибка отправки в {target}: {e}"

    async def send_file(self, target: str, path: str, caption: str = "") -> str:
        """Отправляет файл/фото с диска в чат (только Создатель: файл читается с его ПК)."""
        if not self._pc_allowed():
            return "Отправка файлов с компьютера доступна только Создателю в личном чате."
        file = Path(path).resolve()
        if not file.is_file():
            return f"Файл не найден: {path}"
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            caption = caption[:1024]
            sent = await self._send(lambda: self.client.send_file(entity, str(file), caption=caption))
            await self.db.save_message(0, "ada", f"[файл {file.name}] {caption}".strip(), chat_id=get_peer_id(entity),
                                       sender_name="Ада", tg_message_id=getattr(sent, "id", None))
            return f"Файл {file.name} отправлен в {target}."
        except Exception as e:
            return f"Не удалось отправить файл: {e}"

    async def remember_scoped_fact(self, scope: str, content: str, category: str = "fact",
                                   owner_id: int = 0, chat_id: int = 0, importance: int = 5) -> str:
        if not self._memory_write_context.get():
            return "Не сохранено: память изменяется только по явной просьбе собеседника."
        actor_id = self._actor_context.get()
        current_chat = self._chat_context.get()
        if scope == "global" and not self._is_admin():
            return "Только владелец может добавлять глобальные воспоминания."
        safe_scope = scope if scope in {"global", "person", "group"} else "person"
        await self.db.add_scoped_memory(
            safe_scope, content, category,
            owner_id=None if safe_scope == "global" else actor_id,
            chat_id=current_chat if safe_scope == "group" else None,
            importance=importance,
            confidence=1.0,
            source="explicit_request",
        )
        return "Факт сохранён с источником — явная просьба пользователя."


    async def search_scoped_memory(self, query: str, owner_id: int = 0, chat_id: int = 0) -> str:
        rows = await self.db.get_relevant_memories(
            query,
            owner_id=self._actor_context.get(),
            chat_id=self._chat_context.get(),
            is_group=self._chat_context.get() < 0,
            limit=8,
        )
        return "\n".join(
            f"#{row['id']} [{row['scope']}/{row['category']}; confidence={row['confidence']:.2f}] {row['content']}"
            for row in rows if float(row.get("confidence", 0)) >= 0.8
        ) or "Подтверждённых совпадений в памяти не найдено."


    async def set_person_category(self, user_id: int, category: str) -> str:
        if not self._is_admin() or not self._memory_write_context.get():
            return "Изменять категорию людей может только администратор."
        if category not in {"стандарт", "друг", "админ"}:
            return "Категория должна быть: стандарт, друг или админ."
        if category == "админ" and user_id != config.ADMIN_USER_ID:
            return "Добавлять новых администраторов через модель запрещено."
        await self.db.set_user_category(user_id, category)
        return f"Категория пользователя {user_id} изменена на «{category}»."

    async def set_person_respect(self, user_id: int, respect: int) -> str:
        if not self._is_admin() or not self._memory_write_context.get():
            return "Управлять уважением может только администратор."
        await self.db.update_user_relationship(user_id, {"respect": max(0, min(100, int(respect)))})
        return f"Уважение пользователя {user_id}: {max(0, min(100, int(respect)))}."

    async def set_mood(self, emotion: str, value: int) -> str:
        if not self._is_admin() or not self._memory_write_context.get() or self.emotions is None:
            return "Изменять настроение может только администратор."
        await self.emotions.set_emotion(emotion, max(0, min(100, int(value))))
        return f"Эмоция {emotion} установлена в {max(0, min(100, int(value)))}."

    async def clear_memory(self) -> str:
        if not self._is_admin():
            return "Очищать память может только администратор."
        await self.db.clear_memory()
        return "Память очищена полностью."

    async def remember_fact(self, category: str, content: str) -> str:
        """Сохраняет неподтверждённый кандидат памяти, не выдавая его за факт."""
        if not self._memory_write_context.get():
            return "Не сохранено: память изменяется только по явной просьбе собеседника."
        await self.db.add_scoped_memory(
            "person" if self._actor_context.get() else "global",
            content,
            category,
            owner_id=self._actor_context.get() or None,
            chat_id=self._chat_context.get() or None,
            importance=7,
            confidence=1.0,
            source="explicit_user",
        )
        return f"Запомнила подтверждённый факт: [{category}] {content}"

    async def search_memory(self, query: str) -> str:
        """Ищет совпадения в сохраненных воспоминаниях."""
        mems = await self.db.get_all_memories()
        matched = [m['content'] for m in mems if query.lower() in m['content'].lower()]
        if matched:
            return "Найдено в памяти:\n" + "\n".join(f"- {m}" for m in matched[:6])
        return "Ничего не найдено в памяти."
