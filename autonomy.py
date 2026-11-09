import asyncio
from datetime import datetime
import logging
from config import AUTONOMY_ENABLED, AUTONOMY_CHECK_INTERVAL, AUTONOMY_SILENCE_MINUTES, QUIET_HOURS_START, QUIET_HOURS_END
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

                    # Кандидат найден
                    is_admin = target.get("status") == "creator"
                    prompt = (
                        "Внутренний импульс автономии: ты давно не писала этому человеку. "
                        "Посмотри историю общения и реши, хочешь ли ты написать ему первой: "
                        "спросить как дела, продолжить старую тему или поделиться мыслью. "
                        "Если писать сейчас неуместно — ответь ровно словом NO_REPLY."
                    )

                    decision = await self.brain.process_interaction(user_id, prompt, is_admin=is_admin)
                    reply_text = decision.get("reply", "").strip()

                    if reply_text and "NO_REPLY" not in reply_text:
                        logger.info(f"Автономная отправка первого сообщения для {user_id}")
                        await self.send_message(user_id, reply_text)
                        await self.db.save_message(user_id, "ada", reply_text)
                        break

            except Exception as e:
                logger.exception(f"Исключение в модуле автономии: {e}")

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
