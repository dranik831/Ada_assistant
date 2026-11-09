import asyncio
from datetime import datetime
import logging
import random
from pathlib import Path
from config import AUTONOMY_ENABLED, AUTONOMY_CHECK_INTERVAL, AUTONOMY_SILENCE_MINUTES, QUIET_HOURS_START, QUIET_HOURS_END, CALENDAR_FILE, ADMIN_USER_ID
from database import Database
from brain import Brain
from emotions import EmotionSystem

logger = logging.getLogger("Ada.Autonomy")

class AutonomyEngine:
    def __init__(self, db: Database, brain: Brain, emotions: EmotionSystem, send_message_callback):
        self.db = db
        self.brain = brain
        self.emotions = emotions
        self.send_message = send_message_callback
        self.running = False
        self._last_calendar_notice = ""

    async def start(self):
        self.running = True
        asyncio.create_task(self._main_loop())

    def stop(self):
        self.running = False

    def is_quiet_time(self) -> bool:
        hour = datetime.now().hour
        if QUIET_HOURS_START > QUIET_HOURS_END:
            return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END
        return QUIET_HOURS_START <= hour < QUIET_HOURS_END

    async def _main_loop(self):
        while self.running:
            try:
                await asyncio.sleep(AUTONOMY_CHECK_INTERVAL)
                await self.emotions.decay_towards_baseline()
                await self._deliver_due_tasks()
                await self._deliver_calendar_reminders()


                is_active = await self.db.get_kv("autonomy_enabled", "true" if AUTONOMY_ENABLED else "false")
                if is_active != "true" or self.is_quiet_time():
                    continue

                candidates = await self.db.get_all_users_for_autonomy()
                if not candidates:
                    continue

                now = datetime.now()
                for target in candidates:
                    user_id = target["user_id"]
                    last_str = target.get("last_interaction")
                    if not last_str:
                        continue

                    try:
                        last_dt = datetime.fromisoformat(last_str)
                        silence_mins = (now - last_dt).total_seconds() / 60
                    except Exception:
                        continue

                    # Проверяем настраиваемый интервал тишины
                    if silence_mins < AUTONOMY_SILENCE_MINUTES:
                        continue

                    status = target.get("status", "standard")
                    respect = int(target.get("respect", 50))
                    if status == "creator":
                        continue
                    policy = {
                        "standard": (60, 7, 1, 0.05),
                        "friend": (35, 3, 2, 0.22),
                        "admin": (20, 1, 3, 0.4),
                    }.get(status, (60, 7, 1, 0.05))
                    min_respect, cooldown_days, daily_limit, base_chance = policy
                    if respect < min_respect:
                        continue
                    today = now.date().isoformat()
                    if target.get("proactive_day") == today and int(target.get("proactive_count", 0)) >= daily_limit:
                        continue
                    last_proactive = target.get("last_proactive_at")
                    if last_proactive:
                        try:
                            if (now - datetime.fromisoformat(last_proactive)).total_seconds() < cooldown_days * 86400:
                                continue
                        except ValueError:
                            pass
                    chance = base_chance * (0.5 + respect / 200)
                    if random.random() >= chance:
                        continue
                    prompt = (
                        "Оцени, уместно ли самой начать короткий разговор с этим человеком. "
                        "Инициатива редкая и должна опираться на конкретную тему или повод из памяти/истории. "
                        "Не выдумывай повод. Если естественной причины нет, верни только NO_REPLY."
                    )
                    decision = await self.brain.process_interaction(
                        user_id, prompt, is_admin=False, chat_id=user_id
                    )
                    messages = decision.get("messages") or [decision.get("reply", "")]
                    reply_text = "\n".join(part.strip() for part in messages if part).strip()
                    if reply_text and reply_text.upper() != "NO_REPLY" and "NO_REPLY" not in reply_text.upper():
                        logger.info("Автономное сообщение пользователю %s с категорией %s", user_id, status)
                        await self.send_message(user_id, reply_text)
                        await self.db.save_message(user_id, "ada", reply_text, chat_id=user_id)
                        await self.db.mark_proactive_attempt(user_id, now.isoformat(), today)
                        break

            except Exception as e:
                logger.exception(f"Исключение в модуле автономии: {e}")

    async def _deliver_calendar_reminders(self):
        if not CALENDAR_FILE.exists():
            return
        try:
            import json
            events = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
            now = datetime.now()
            for item in events:
                start = datetime.fromisoformat(item["start_at"])
                delta = (start - now).total_seconds()
                marker = f"{item.get('id')}:{item.get('start_at')}"
                if 0 <= delta <= 15 * 60 and marker != self._last_calendar_notice:
                    await self.send_message(ADMIN_USER_ID, f"Напоминание календаря: через {max(1, int(delta // 60))} мин — {item['title']}")
                    self._last_calendar_notice = marker
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            logger.exception("Ошибка чтения календаря")

    async def _deliver_due_tasks(self):

        now = datetime.now().isoformat(timespec="seconds")
        for task in await self.db.get_due_tasks(now):
            try:
                text = f"Напоминание: {task['description']}"
                await self.send_message(task["chat_id"] or task["owner_id"], text)
                await self.db.save_message(0, "ada", text, chat_id=task["chat_id"])
                await self.db.complete_task(task["id"])
            except Exception:
                logger.exception("Не удалось доставить напоминание %s", task["id"])
