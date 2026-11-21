"""Модуль секретаря: личный календарь, импортированное расписание, напоминания
и запросы на встречу с Создателем.

Включение: MODULE_SECRETARY=true в .env.

Настройки .env:
  CALENDAR_FILE=data/calendar.json
  SCHEDULE_FILE=data/schedule.json

Даёт инструменты:
  add_calendar_event, list_calendar_events, delete_calendar_event,
  import_schedule_json, read_schedule,
  create_reminder, list_tasks, cancel_task,
  create_meeting_request, list_meeting_requests
и хук autonomy_tick для напоминаний о приближающихся встречах в календаре.
"""

import json
import logging
from datetime import datetime

from module_system import HOOK_AUTONOMY_TICK, ModuleContext

import config

DESCRIPTION = "секретарь: календарь, расписание, напоминания, запросы на встречу"
VERSION = "1.0"
DEFAULT_ENABLED = True

logger = logging.getLogger("Ada.Module.secretary")

__all__ = ["setup"]


def setup(ctx: ModuleContext) -> None:
    db = ctx.db

    async def add_calendar_event(owner_id: int, title: str, start_at: str,
                                 end_at: str = "", details: str = "") -> str:
        """Добавляет событие в локальный календарь секретаря."""
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Добавлять события в календарь Драника может только Создатель."
        path = config.CALENDAR_FILE
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

    async def list_calendar_events(from_at: str = "", to_at: str = "") -> str:
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Календарь Драника доступен только Создателю."
        path = config.CALENDAR_FILE
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
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return f"Ошибка чтения календаря: {exc}"

    async def delete_calendar_event(event_id: int) -> str:
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Удалять встречи может только Создатель."
        path = config.CALENDAR_FILE
        try:
            events = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            updated = [item for item in events if int(item.get("id", -1)) != int(event_id)]
            if len(updated) == len(events):
                return "Событие не найдено."
            path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            return "Событие удалено из календаря."
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return f"Ошибка удаления встречи: {exc}"

    async def import_schedule_json(content: str) -> str:
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Загружать расписание может только Создатель."
        try:
            data = json.loads(content)
            if not isinstance(data, (list, dict)):
                return "Расписание должно быть JSON-массивом или объектом."
            path = config.SCHEDULE_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return "Расписание сохранено."
        except (json.JSONDecodeError, OSError) as exc:
            return f"Не удалось сохранить расписание: {exc}"

    async def read_schedule() -> str:
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Личное расписание доступно только Создателю."
        path = config.SCHEDULE_FILE
        if not path.exists():
            return "Расписание пока не импортировано. Нужен JSON-экспорт расписания."
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return json.dumps(data, ensure_ascii=False, indent=2)[:8000]
        except (OSError, json.JSONDecodeError) as exc:
            return f"Ошибка чтения расписания: {exc}"

    async def create_reminder(owner_id: int, description: str, due_at: str,
                              chat_id: int = 0, repeat_rule: str = "") -> str:
        if ctx.tools is None or not ctx.tools._is_admin() or int(owner_id) != config.ADMIN_USER_ID:
            return "Создавать напоминания в личном календаре может только Создатель."
        try:
            datetime.fromisoformat(due_at)
        except ValueError:
            return "Неверная дата. Используй ISO-формат: 2026-11-08T15:00:00"
        safe_repeat = repeat_rule.strip().lower() if repeat_rule else ""
        if safe_repeat not in {"", "daily", "weekly", "weekdays"}:
            return "repeat_rule может быть: daily, weekly, weekdays или пусто (одноразово)."
        task_id = await db.create_task(owner_id, description, due_at, chat_id or None, repeat_rule=safe_repeat)
        return f"Напоминание создано, ID={task_id}, время={due_at}" + (f", повтор: {safe_repeat}" if safe_repeat else "") + "."

    async def list_tasks(owner_id: int = 0) -> str:
        actor_id = ctx.tools._actor_context.get() if ctx.tools else 0
        rows = await db.list_tasks(owner_id or actor_id)
        return "\n".join(f"#{row['id']} {row['due_at']} — {row['description']}" for row in rows) or "Активных задач нет."

    async def cancel_task(task_id: int) -> str:
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Отменять напоминания может только Создатель."
        cancelled = await db.cancel_task(int(task_id))
        return "Напоминание отменено." if cancelled else "Задача не найдена или уже выполнена."

    async def create_meeting_request(requester_id: int, requester_name: str, purpose: str,
                                     preferred_time: str = "") -> str:
        import aiosqlite
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("""
                INSERT INTO meeting_requests(requester_id, requester_name, purpose, preferred_time)
                VALUES (?, ?, ?, ?)
            """, (requester_id, requester_name[:200], purpose[:2000], preferred_time[:300]))
            await conn.commit()
        return "Запрос на встречу сохранён и передан Дранику на рассмотрение."

    async def list_meeting_requests() -> str:
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Просматривать запросы может только Создатель."
        import aiosqlite
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM meeting_requests WHERE status='pending' ORDER BY id DESC LIMIT 30")
            rows = await cursor.fetchall()
        return "\n".join(
            f"#{r['id']} {r['requester_name']} ({r['requester_id']}): {r['purpose']}; время: {r['preferred_time'] or 'не указано'}"
            for r in rows
        ) or "Новых запросов нет."

    _last_calendar_notice = {"marker": ""}

    async def on_autonomy_tick(autonomy) -> None:
        """Напоминание за 15 минут до события календаря (перенесено из autonomy.py)."""
        path = config.CALENDAR_FILE
        if not path.exists():
            return
        try:
            events = json.loads(path.read_text(encoding="utf-8"))
            now = datetime.now()
            for item in events:
                start = datetime.fromisoformat(item["start_at"])
                if start.tzinfo is not None:
                    start = start.astimezone().replace(tzinfo=None)
                delta = (start - now).total_seconds()
                marker = f"{item.get('id')}:{item.get('start_at')}"
                if 0 <= delta <= 15 * 60 and marker != _last_calendar_notice["marker"]:
                    await autonomy.send_message(
                        config.ADMIN_USER_ID,
                        f"Напоминание календаря: через {max(1, int(delta // 60))} мин — {item['title']}",
                    )
                    _last_calendar_notice["marker"] = marker
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            logger.exception("Ошибка чтения календаря")

    action_specs = [
        ("add_calendar_event", add_calendar_event, "Добавить событие в календарь владельца.",
         {"owner_id": {"type": "integer"}, "title": {"type": "string"}, "start_at": {"type": "string"},
          "end_at": {"type": "string"}, "details": {"type": "string"}}, ["owner_id", "title", "start_at"]),
        ("list_calendar_events", list_calendar_events, "Показать события календаря.",
         {"from_at": {"type": "string"}, "to_at": {"type": "string"}}, []),
        ("delete_calendar_event", delete_calendar_event, "Удалить встречу по ID.",
         {"event_id": {"type": "integer"}}, ["event_id"]),
        ("import_schedule_json", import_schedule_json, "Импортировать расписание из JSON.",
         {"content": {"type": "string"}}, ["content"]),
        ("read_schedule", read_schedule, "Прочитать импортированное расписание.", {}, []),
        ("create_reminder", create_reminder,
         "Создать напоминание в ISO-времени; repeat_rule — daily, weekly или weekdays для повторяющихся. "
         "В срок тебе придёт служебный пинок с этим description, поэтому пиши его как инструкцию себе: что "
         "именно сделать и какими инструментами (например: «Вызови get_college_schedule на сегодня и отправь "
         "Создателю само расписание»).",
         {"owner_id": {"type": "integer"}, "description": {"type": "string"}, "due_at": {"type": "string"},
          "chat_id": {"type": "integer"}, "repeat_rule": {"type": "string"}}, ["owner_id", "description", "due_at"]),
        ("list_tasks", list_tasks, "Показать активные напоминания.", {"owner_id": {"type": "integer"}}, []),
        ("cancel_task", cancel_task, "Отменить напоминание по ID (только Создатель).",
         {"task_id": {"type": "integer"}}, ["task_id"]),
        ("create_meeting_request", create_meeting_request, "Сохранить запрос на встречу, не подтверждая её.",
         {"requester_id": {"type": "integer"}, "requester_name": {"type": "string"}, "purpose": {"type": "string"},
          "preferred_time": {"type": "string"}}, ["requester_id", "requester_name", "purpose"]),
        ("list_meeting_requests", list_meeting_requests, "Показать владельцу необработанные запросы на встречу.",
         {}, []),
    ]
    for name, func, description, properties, required in action_specs:
        ctx.register_tool(name, func, description, properties, required)

    ctx.register_hook(HOOK_AUTONOMY_TICK, on_autonomy_tick)
    ctx.register_capability(
        "add_calendar_event / list_calendar_events / delete_calendar_event / import_schedule_json / "
        "read_schedule / create_reminder / list_tasks / cancel_task / create_meeting_request / "
        "list_meeting_requests: секретарские функции — календарь, расписание, напоминания, встречи."
    )
