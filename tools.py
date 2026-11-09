import os
import subprocess
import asyncio
import logging
import json
from datetime import datetime
from contextvars import ContextVar
from pathlib import Path
import aiohttp
import trafilatura
from telethon import TelegramClient
from telethon.tl.functions.messages import EditMessageRequest, DeleteMessagesRequest
from telethon.tl.types import User
from telethon.tl.functions.account import UpdateProfileRequest
from database import Database

logger = logging.getLogger("Ada.Tools")

class AgentTools:
    def __init__(self, db: Database, tg_client: TelegramClient):
        self.db = db
        self.client = tg_client
        self._admin_context: ContextVar[bool] = ContextVar("ada_admin_context", default=False)

    def set_admin_context(self, is_admin: bool):
        self._admin_context.set(bool(is_admin))

    def _is_admin(self) -> bool:
        return self._admin_context.get()

    async def list_my_capabilities(self) -> str:
        """Возвращает перечень доступных возможностей Ады."""
        return (
            "Мои инструменты:\n"
            "1. execute_system_command: выполнение команд в терминале ОС (только Создатель).\n"
            "2. read_file / write_file / list_directory: работа с файлами на компьютере.\n"
            "3. fetch_web_page: чтение статей по ссылкам в интернете.\n"
            "4. send_message_to_user: отправка сообщений пользователям и группам.\n"
            "5. find_contact: поиск контактов в Telegram и локальной базе.\n"
            "6. remember_fact / search_memory: долговременная память.\n"
            "7. update_my_profile: изменение имени и описания аккаунта."
            "\n8. list_dialogs / read_chat_history / search_telegram: работа с диалогами и историей."
        )

    async def execute_system_command(self, command: str) -> str:
        """Выполняет системную команду в PowerShell/Bash. Доступно только Создателю."""
        if not self._is_admin():
            return "Ошибка: Доступ к терминалу заблокирован политикой безопасности. Команда доступна только Создателю."

        logger.info(f"Исполнение терминальной команды: {command}")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
            
            out_decoded = stdout.decode('cp1251', errors='replace') if os.name == 'nt' else stdout.decode('utf-8', errors='replace')
            err_decoded = stderr.decode('cp1251', errors='replace') if os.name == 'nt' else stderr.decode('utf-8', errors='replace')

            res = []
            if out_decoded.strip():
                res.append(f"STDOUT:\n{out_decoded.strip()}")
            if err_decoded.strip():
                res.append(f"STDERR:\n{err_decoded.strip()}")
            if not res:
                res.append("Команда выполнена успешно (вывод пуст).")
            
            output = "\n".join(res)
            return output[:3000]
        except asyncio.TimeoutError:
            return "Таймаут выполнения команды (превышено 45 секунд)."
        except Exception as e:
            return f"Ошибка запуска процесса: {e}"

    async def read_file(self, path: str) -> str:
        """Читает текстовый файл по указанному пути."""
        try:
            target = Path(path).resolve()
            if not target.exists():
                return f"Файл не найден: {path}"
            if not target.is_file():
                return f"Путь указывает на папку, а не файл: {path}"
            
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(4000)
            return f"Содержимое {target.name}:\n{content}"
        except Exception as e:
            return f"Не удалось прочитать файл: {e}"

    async def write_file(self, path: str, content: str) -> str:
        """Записывает текст в файл (только Создатель)."""
        if not self._is_admin():
            return "Запись файлов разрешена только Создателю."
        try:
            target = Path(path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Файл {target.name} успешно сохранен."
        except Exception as e:
            return f"Ошибка записи файла: {e}"

    async def list_directory(self, path: str = ".") -> str:
        """Возвращает список файлов и папок в директории."""
        try:
            p = Path(path).resolve()
            if not p.exists():
                return f"Папка не найдена: {path}"
            items = []
            for item in list(p.iterdir())[:30]:
                prefix = "[DIR] " if item.is_dir() else "[FILE]"
                items.append(f"{prefix} {item.name}")
            return f"Каталог {p.name}:\n" + "\n".join(items)
        except Exception as e:
            return f"Не удалось открыть каталог: {e}"

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

    async def find_contact(self, query: str) -> str:
        """Ищет пользователя по username или имени в Telegram и локальной базе."""
        try:
            entity = await self.client.get_entity(query)
            if isinstance(entity, User):
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                return f"Telegram пользователь: ID={entity.id}, @{entity.username}, Имя: {name}"
        except Exception:
            pass

        import aiosqlite
        async with aiosqlite.connect(self.db.db_path) as conn:
            cursor = await conn.execute(
                "SELECT user_id, username, first_name, status, respect FROM users WHERE username LIKE ? OR first_name LIKE ?",
                (f"%{query}%", f"%{query}%")
            )
            rows = await cursor.fetchall()
            if rows:
                items = [f"ID={r[0]}, @{r[1]}, Имя: {r[2]}, Статус: {r[3]}, Respect: {r[4]}" for r in rows]
                return "Найдено в базе данных:\n" + "\n".join(items)

        return f"Контакт '{query}' не найден."

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

            await self.client.send_message(entity, message)
            uid = getattr(entity, 'id', 0)
            if uid:
                await self.db.save_message(uid, "ada", message)
            return f"Сообщение успешно отправлено в {target}."
        except Exception as e:
            return f"Ошибка отправки в {target}: {e}"

    async def edit_message(self, target: str, message_id: int, new_text: str) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            await self.client(EditMessageRequest(peer=entity, id=int(message_id), message=new_text[:4096]))
            return f"Сообщение {message_id} отредактировано."
        except Exception as e:
            return f"Не удалось отредактировать сообщение: {e}"

    async def delete_message(self, target: str, message_id: int, revoke: bool = True) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            await self.client.delete_messages(entity, [int(message_id)], revoke=revoke)
            return f"Сообщение {message_id} удалено."
        except Exception as e:
            return f"Не удалось удалить сообщение: {e}"

    async def reply_to_message(self, target: str, message_id: int, message: str) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            await self.client.send_message(entity, message[:4096], reply_to=int(message_id))
            return f"Ответ на сообщение {message_id} отправлен."
        except Exception as e:
            return f"Не удалось ответить на сообщение: {e}"

    async def react_to_message(self, target: str, message_id: int, reaction: str = "👍") -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            await self.client.send_reaction(entity, int(message_id), reaction)
            return f"Реакция {reaction} добавлена."
        except Exception as e:
            return f"Не удалось поставить реакцию: {e}"

    async def forward_message(self, target: str, message_id: int, destination: str) -> str:
        try:
            source = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            dest = await self.client.get_entity(int(destination) if str(destination).lstrip('-').isdigit() else destination)
            await self.client.forward_messages(dest, int(message_id), source)
            return "Сообщение переслано."
        except Exception as e:
            return f"Не удалось переслать сообщение: {e}"

    async def pin_message(self, target: str, message_id: int, notify: bool = False) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            await self.client.pin_message(entity, int(message_id), notify=notify)
            return "Сообщение закреплено."
        except Exception as e:
            return f"Не удалось закрепить сообщение: {e}"

    async def mark_chat_read(self, target: str) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            await self.client.send_read_acknowledge(entity)
            return "Чат отмечен прочитанным."
        except Exception as e:
            return f"Не удалось отметить чат прочитанным: {e}"

    async def get_group_members(self, target: str, limit: int = 50) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            users = await self.client.get_participants(entity, limit=max(1, min(int(limit), 200)))
            return "\n".join(
                f"ID={u.id} @{u.username or '-'} {u.first_name or ''} {u.last_name or ''}".strip()
                for u in users
            ) or "Участники не найдены."
        except Exception as e:
            return f"Не удалось получить участников: {e}"

    async def get_chat_info(self, target: str) -> str:
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip('-').isdigit() else target)
            title = getattr(entity, 'title', None) or getattr(entity, 'first_name', None) or ''
            return f"ID={entity.id}\nНазвание: {title}\nТип: {type(entity).__name__}"
        except Exception as e:
            return f"Не удалось получить информацию о чате: {e}"

    async def remember_scoped_fact(self, scope: str, content: str, category: str = "fact",
                                   owner_id: int = 0, chat_id: int = 0, importance: int = 5) -> str:
        await self.db.add_scoped_memory(scope, content, category, owner_id or None, chat_id or None, importance, source="agent")
        return "Факт сохранён в контекстной памяти."

    async def search_scoped_memory(self, query: str, owner_id: int = 0, chat_id: int = 0) -> str:
        rows = await self.db.search_scoped_memories(query, owner_id or None, chat_id or None)
        return "\n".join(f"#{r['id']} [{r['scope']}/{r['category']}] {r['content']}" for r in rows) or "В памяти ничего не найдено."

    async def create_reminder(self, owner_id: int, description: str, due_at: str, chat_id: int = 0) -> str:
        task_id = await self.db.create_task(owner_id, description, due_at, chat_id or None)
        return f"Напоминание создано, ID={task_id}, время={due_at}."

    async def add_calendar_event(self, owner_id: int, title: str, start_at: str,
                                 end_at: str = "", details: str = "") -> str:
        """Добавляет событие в локальный календарь секретаря."""
        path = Path(__import__("config").CALENDAR_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            datetime.fromisoformat(start_at)
            if end_at:
                datetime.fromisoformat(end_at)
        except ValueError:
            return "Неверная дата. Используй ISO-формат: 2026-11-08T15:00:00"
        try:
            events = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, json.JSONDecodeError):
            events = []
        next_id = max((int(item.get("id", 0)) for item in events), default=0) + 1
        events.append({"id": next_id, "owner_id": owner_id, "title": title[:300],
                       "start_at": start_at, "end_at": end_at, "details": details[:2000]})
        path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"Событие добавлено в календарь, ID={next_id}."

    async def list_calendar_events(self, from_at: str = "", to_at: str = "") -> str:
        path = Path(__import__("config").CALENDAR_FILE)
        if not path.exists():
            return "Календарь пуст."
        try:
            events = json.loads(path.read_text(encoding="utf-8"))
            start = datetime.fromisoformat(from_at) if from_at else None
            end = datetime.fromisoformat(to_at) if to_at else None
            result = []
            for item in events:
                point = datetime.fromisoformat(item["start_at"])
                if start and point < start or end and point > end:
                    continue
                result.append(f"#{item['id']} {item['start_at']} — {item['title']} {item.get('details', '')}")
            return "\n".join(result) or "На этот период событий нет."
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return f"Ошибка чтения календаря: {e}"

    async def import_schedule_json(self, content: str) -> str:
        """Заменяет расписание из JSON, полученного от владельца или парсера."""
        try:
            data = json.loads(content)
            if not isinstance(data, (list, dict)):
                return "Расписание должно быть JSON-массивом или объектом."
            path = Path(__import__("config").SCHEDULE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return "Расписание сохранено."
        except (json.JSONDecodeError, OSError) as e:
            return f"Не удалось сохранить расписание: {e}"

    async def list_dialogs(self, limit: int = 30) -> str:
        """Возвращает список последних диалогов аккаунта."""
        try:
            dialogs = await self.client.get_dialogs(limit=max(1, min(int(limit), 100)))
            lines = []
            for dialog in dialogs:
                entity = dialog.entity
                title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Без названия"
                lines.append(f"ID={dialog.id} | {title} | непрочитано={dialog.unread_count}")
            return "\n".join(lines) or "Диалоги не найдены."
        except Exception as e:
            return f"Ошибка получения диалогов: {e}"

    async def read_chat_history(self, target: str, limit: int = 20) -> str:
        """Читает последние сообщения указанного чата."""
        try:
            entity = await self.client.get_entity(int(target) if str(target).lstrip("-").isdigit() else target)
            messages = await self.client.get_messages(entity, limit=max(1, min(int(limit), 100)))
            lines = []
            for message in reversed(messages):
                sender = await message.get_sender()
                name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Неизвестно"
                lines.append(f"[{message.id}] {name}: {message.text or '[медиа]'}")
            return "\n".join(lines) or "История пуста."
        except Exception as e:
            return f"Ошибка чтения истории: {e}"

    async def search_telegram(self, query: str, limit: int = 20) -> str:
        """Ищет сообщения по всему Telegram, доступные аккаунту."""
        try:
            messages = await self.client.get_messages(
                None, search=query, limit=max(1, min(int(limit), 100))
            )
            lines = [f"[{m.id}] chat={getattr(m, 'peer_id', '')}: {m.text or '[медиа]'}" for m in messages]
            return "\n".join(lines) or "Совпадений нет."
        except Exception as e:
            return f"Ошибка поиска: {e}"

    async def remember_fact(self, category: str, content: str) -> str:
        """Записывает важный факт или событие в долговременную память."""
        await self.db.add_memory(category, content, importance=7)
        return f"Запомнила: [{category}] {content}"

    async def search_memory(self, query: str) -> str:
        """Ищет совпадения в сохраненных воспоминаниях."""
        mems = await self.db.get_all_memories()
        matched = [m['content'] for m in mems if query.lower() in m['content'].lower()]
        if matched:
            return "Найдено в памяти:\n" + "\n".join(f"- {m}" for m in matched[:6])
        return "Ничего не найдено в памяти."
