"""Модуль расписания колледжа: клиент учебного портала УКРТБ (study.ukrtb.ru).

Включение: MODULE_SCHEDULE_COLLEGE=true в .env.

Настройки .env:
  UKRTB_GROUP_ID=
  UKRTB_GROUP_LABEL=

Даёт:
  инструменты get_college_schedule, refresh_my_schedule_cache;
  хук autonomy_tick — утреннее расписание Создателю раз в день.
"""

import logging
import re
from datetime import datetime, timedelta

from module_system import HOOK_AUTONOMY_TICK, ModuleContext

import config

from . import api as schedule_api

DESCRIPTION = "расписание колледжа УКРТБ: занятия, звонки, утренняя сводка"
VERSION = "1.0"
DEFAULT_ENABLED = True

logger = logging.getLogger("Ada.Module.schedule_college")

_SILENCE_RE = re.compile(r"^\s*(SILENCE|NO_REPLY|МОЛЧУ|МОЛЧАНИЕ)\s*$", re.IGNORECASE)

__all__ = ["schedule_api", "setup"]


def _parse_schedule_date(when: str) -> str:
    """Принимает ISO-дату, 'сегодня', 'завтра', 'послезавтра'; возвращает YYYY-MM-DD."""
    value = (when or "").strip().lower()
    today = datetime.now().date()
    if value in {"", "сегодня", "today"}:
        return today.isoformat()
    if value in {"завтра", "tomorrow"}:
        return (today + timedelta(days=1)).isoformat()
    if value in {"послезавтра"}:
        return (today + timedelta(days=2)).isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def setup(ctx: ModuleContext) -> None:
    async def get_college_schedule(when: str = "", query: str = "") -> str:
        """Расписание колледжа с учебного портала УКРТБ (только Создатель)."""
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Расписание колледжа доступно только Создателю."
        try:
            target_date = _parse_schedule_date(when)
        except ValueError:
            return "Неверная дата. Форматы: 'сегодня', 'завтра' или 2026-11-13."
        list_type = "groups"
        target = query or config.UKRTB_GROUP_ID or config.UKRTB_GROUP_LABEL
        if query:
            lowered = query.lower()
            if any(word in lowered for word in ("преподав", "учитель")):
                list_type = "teachers"
            elif any(word in lowered for word in ("аудитор", "кабинет")):
                list_type = "audiences"
        data = await schedule_api.get_schedule(target, target_date, list_type=list_type)
        if not data:
            cached = schedule_api.get_cached_schedule()
            if cached and not query and (cached.get("date", {}).get("date") or "").startswith(
                datetime.strptime(target_date, "%Y-%m-%d").strftime("%d ")
            ):
                return schedule_api.format_schedule(cached, target_date) + "\n(из кэша: сайт недоступен)"
            return "Не удалось получить расписание: группа не найдена или сайт недоступен."
        return schedule_api.format_schedule(data, target_date)

    async def refresh_my_schedule_cache() -> str:
        """Обновляет локальный кэш расписания настроенной группы УКРТБ."""
        if ctx.tools is None or not ctx.tools._is_admin():
            return "Обновлять кэш расписания может только Создатель."
        data = await schedule_api.get_my_schedule()
        if not data:
            return "Сайт УКРТБ недоступен или группа не найдена."
        return "Кэш расписания обновлён:\n" + schedule_api.format_schedule(data)[:3500]

    async def on_autonomy_tick(autonomy) -> None:
        """Раз в день (7:00-14:00) отправляет Создателю расписание на сегодня."""
        today = datetime.now().date().isoformat()
        sent_day = await autonomy.db.get_kv("morning_schedule_day", "")
        if sent_day == today:
            return
        hour = datetime.now().hour
        if not (7 <= hour < 14) or autonomy.is_quiet_time():
            return
        try:
            data = await schedule_api.get_my_schedule()
            if not data:
                return
            text = schedule_api.format_schedule(data)
            await autonomy.send_message(config.ADMIN_USER_ID, f"Расписание на сегодня:\n{text}")
            await autonomy.db.set_kv("morning_schedule_day", today)
        except Exception:
            logger.exception("Не удалось отправить утреннее расписание")

    ctx.register_tool(
        "get_college_schedule",
        get_college_schedule,
        "Расписание колледжа УКРТБ на дату: 'сегодня', 'завтра' или YYYY-MM-DD; query — название группы, "
        "преподавателя или аудитории (пусто — группа Создателя).",
        {"when": {"type": "string"}, "query": {"type": "string"}},
        [],
    )
    ctx.register_tool(
        "refresh_my_schedule_cache",
        refresh_my_schedule_cache,
        "Обновить локальный кэш расписания группы УКРТБ с сайта.",
        {},
        [],
    )
    ctx.register_hook(HOOK_AUTONOMY_TICK, on_autonomy_tick)
    ctx.register_capability(
        "get_college_schedule / refresh_my_schedule_cache: расписание колледжа УКРТБ; "
        "утренняя сводка приходит Создателю автоматически."
    )
