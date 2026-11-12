"""Клиент учебного портала УКРТБ (study.ukrtb.ru).

Портал отдаёт расписание через открытый JSON-API без авторизации:
  GET /api/frontend/schedule/get/lists/{groups|teachers|audiences}
  GET /api/frontend/schedule/get?type=groups&item[value]=<uuid>&date=YYYY-MM-DD
  GET /api/frontend/schedule/get/border/date
  GET /api/frontend/schedule/get/timetables

Модуль кэширует ответ в data/schedule_cache.json, чтобы расписание
было доступно Аде даже при недоступности сайта.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp

import config

logger = logging.getLogger("Ada.Schedule")

BASE_URL = "https://study.ukrtb.ru"
CACHE_TTL_SECONDS = 30 * 60

TYPE_LABELS = {
    "groups": "группы",
    "teachers": "преподаватели",
    "audiences": "аудитории",
}

_cache_lock = asyncio.Lock()
_lists_cache: dict[str, tuple[float, list]] = {}


async def _get_json(path: str, params: dict | None = None) -> object | None:
    url = f"{BASE_URL}{path}"
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/schedule",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 204:
                    return None
                if resp.status != 200:
                    logger.warning("UKRTB API %s вернул HTTP %s", path, resp.status)
                    return None
                return await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("UKRTB API %s недоступен: %s", path, exc)
        return None


def _cache_path() -> Path:
    return config.DATA_DIR / "schedule_cache.json"


def _read_cache() -> dict | None:
    path = _cache_path()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data.get("fetched_at", ""))
        if (datetime.now() - fetched).total_seconds() > CACHE_TTL_SECONDS * 8:
            return None
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_cache(payload: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({**payload, "fetched_at": datetime.now().isoformat(timespec="seconds")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("Не удалось сохранить кэш расписания")


async def get_lists(list_type: str = "groups") -> list:
    """Список элементов выбора: [{label, value}]."""
    list_type = list_type if list_type in TYPE_LABELS else "groups"
    now = asyncio.get_running_loop().time()
    cached = _lists_cache.get(list_type)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    data = await _get_json(f"/api/frontend/schedule/get/lists/{list_type}")
    items = data.get("lists", []) if isinstance(data, dict) else []
    if items:
        _lists_cache[list_type] = (now, items)
    return items


async def resolve_item(query: str, list_type: str = "groups") -> dict | None:
    """Находит элемент списка по точному или частичному совпадению названия."""
    query = (query or "").strip().lower()
    if not query:
        return None
    items = await get_lists(list_type)
    for item in items:
        if item.get("value") == query or item.get("label", "").lower() == query:
            return item
    partial = [item for item in items if query in item.get("label", "").lower()]
    return partial[0] if partial else None


async def get_schedule(query: str, when: str = "", list_type: str = "groups") -> dict | None:
    """Расписание для группы/преподавателя/аудитории на дату YYYY-MM-DD."""
    item = await resolve_item(query, list_type)
    if not item:
        return None
    params = {
        "type": list_type,
        "item[value]": item["value"],
        "date": when or date.today().isoformat(),
    }
    data = await _get_json("/api/frontend/schedule/get", params)
    if not isinstance(data, dict) or not data.get("success"):
        return None
    data["_item_label"] = item.get("label", "")
    return data


async def get_bells() -> list:
    """Расписание звонков: [{'name': ..., 'timetables': [{'number','start','end'}]}]."""
    data = await _get_json("/api/frontend/schedule/get/timetables")
    if isinstance(data, dict) and data.get("success"):
        return data.get("bells", [])
    return []


def format_schedule(data: dict | None, when: str = "") -> str:
    """Человекочитаемый текст расписания для отправки в Telegram."""
    if not data:
        return "Расписание не найдено или сайт недоступен."
    schedules = data.get("schedules") or []
    date_info = data.get("date") or {}
    label = data.get("label") or data.get("_item_label") or ""
    header = f"{label} — {date_info.get('date', when)} ({date_info.get('day_of_week', '')})".strip()
    if not schedules:
        return f"{header}\nЗанятий нет — можно отдыхать."
    lines = [header]
    for lesson in sorted(schedules, key=lambda s: int(s.get("number", 0) or 0)):
        t = lesson.get("time") or {}
        disc = (lesson.get("discipline") or {}).get("name", "?")
        teacher = (lesson.get("teacher") or {}).get("name", "")
        cab = (lesson.get("cab") or {}).get("cab", "")
        lesson_type = lesson.get("type", "")
        subgroup = lesson.get("subgroup") or 0
        line = f"{lesson.get('number', '')}. {t.get('start', '')}–{t.get('end', '')} {disc}"
        details = []
        if lesson_type:
            details.append(lesson_type)
        if teacher:
            details.append(teacher)
        if cab:
            details.append(f"каб. {cab}")
        if subgroup:
            details.append(f"подгр. {subgroup}")
        if details:
            line += " (" + ", ".join(details) + ")"
        lines.append(line)
    return "\n".join(lines)


async def get_my_schedule(when: str = "") -> dict | None:
    """Расписание настроенной группы Создателя (config.UKRTB_GROUP_ID)."""
    group_id = config.UKRTB_GROUP_ID or config.UKRTB_GROUP_LABEL
    if not group_id:
        return None
    data = await get_schedule(group_id, when, list_type="groups")
    if data:
        _write_cache({"group": config.UKRTB_GROUP_LABEL, "schedule": data})
    return data


def get_cached_schedule() -> dict | None:
    cache = _read_cache()
    if cache:
        return cache.get("schedule")
    return None


def next_lesson_text() -> str:
    """Ближайший оставшийся урок из кэша на сегодня (для автономных подсказок)."""
    data = get_cached_schedule()
    if not data:
        return ""
    now = datetime.now()
    today = now.strftime("%d.%m.%Y")
    for lesson in sorted(data.get("schedules") or [], key=lambda s: int(s.get("number", 0) or 0)):
        if lesson.get("date") != today:
            continue
        try:
            start = datetime.strptime(f"{today} {lesson['time']['start']}", "%d.%m.%Y %H:%M")
        except (KeyError, ValueError):
            continue
        if start > now:
            disc = (lesson.get("discipline") or {}).get("name", "?")
            cab = (lesson.get("cab") or {}).get("cab", "")
            extra = f", каб. {cab}" if cab else ""
            return f"Ближайшее занятие: {start.strftime('%H:%M')} — {disc}{extra}"
    return ""


def is_study_day(now: datetime | None = None) -> bool:
    """Проверка, что сегодня есть занятия по кэшу расписания."""
    data = get_cached_schedule()
    if not data:
        return False
    target = (now or datetime.now()).strftime("%d.%m.%Y")
    return any(lesson.get("date") == target for lesson in data.get("schedules") or [])


def default_group_info() -> str:
    return f"{config.UKRTB_GROUP_LABEL or 'не задана'} (id: {config.UKRTB_GROUP_ID or 'не задан'})"


def week_dates(start: date | None = None, days: int = 7) -> list[str]:
    begin = start or date.today()
    return [(begin + timedelta(offset=i)).isoformat() for i in range(days)]
