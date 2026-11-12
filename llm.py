import os
import json
import logging
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError, RateLimitError
from tools import AgentTools
import config

logger = logging.getLogger("Ada.LLM")

class UniversalAgent:
    def __init__(self, tools_instance: AgentTools):
        self.provider = config.LLM_PROVIDER
        self.base_url = config.ACTIVE_LLM_BASE_URL
        self.api_key = config.ACTIVE_LLM_API_KEY
        self.model_name = config.ACTIVE_LLM_MODEL
        self.tools = tools_instance
        
        # Универсальный клиент с заголовками для совместимости
        default_headers = {}
        if "openrouter.ai" in self.base_url:
            default_headers = {
                "HTTP-Referer": "https://github.com/dranik83/ada_tg",
                "X-Title": "Ada Telegram Agent"
            }

        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            default_headers=default_headers,
            timeout=config.LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        self.fallback_client = None
        if self.provider == "openai_compatible" and config.GEMINI_API_KEY:
            self.fallback_client = AsyncOpenAI(
                base_url=config.GEMINI_BASE_URL,
                api_key=config.GEMINI_API_KEY,
                timeout=config.LLM_TIMEOUT_SECONDS,
                max_retries=0,
            )

        self.tool_functions = {
            "list_my_capabilities": self.tools.list_my_capabilities,
            "execute_system_command": self.tools.execute_system_command,
            "read_file": self.tools.read_file,
            "write_file": self.tools.write_file,
            "list_directory": self.tools.list_directory,
            "fetch_web_page": self.tools.fetch_web_page,
            "update_my_profile": self.tools.update_my_profile,
            "find_contact": self.tools.find_contact,
            "send_message_to_user": self.tools.send_message_to_user,
            "edit_message": self.tools.edit_message,
            "delete_message": self.tools.delete_message,
            "reply_to_message": self.tools.reply_to_message,
            "react_to_message": self.tools.react_to_message,
            "forward_message": self.tools.forward_message,
            "pin_message": self.tools.pin_message,
            "mark_chat_read": self.tools.mark_chat_read,
            "get_group_members": self.tools.get_group_members,
            "get_chat_info": self.tools.get_chat_info,
            "list_dialogs": self.tools.list_dialogs,
            "read_chat_history": self.tools.read_chat_history,
            "search_telegram": self.tools.search_telegram,
            "search_memory": self.tools.search_memory,
            "search_scoped_memory": self.tools.search_scoped_memory,

            "create_reminder": self.tools.create_reminder,
            "add_calendar_event": self.tools.add_calendar_event,
            "list_calendar_events": self.tools.list_calendar_events,
            "import_schedule_json": self.tools.import_schedule_json,
            "read_schedule": self.tools.read_schedule,
            "list_tasks": self.tools.list_tasks,
            "delete_calendar_event": self.tools.delete_calendar_event,
            "cancel_task": self.tools.cancel_task,
            "get_college_schedule": self.tools.get_college_schedule,
            "refresh_my_schedule_cache": self.tools.refresh_my_schedule_cache,
            "create_meeting_request": self.tools.create_meeting_request,
            "list_meeting_requests": self.tools.list_meeting_requests,
            "set_person_category": self.tools.set_person_category,
            "set_person_respect": self.tools.set_person_respect,
            "set_mood": self.tools.set_mood,
        }


    async def _completion(self, *, startup=False, **kwargs):
        client = self.client.with_options(timeout=config.LLM_STARTUP_TIMEOUT_SECONDS) if startup else self.client
        try:
            return await client.chat.completions.create(model=self.model_name, **kwargs)
        except (APIConnectionError, APITimeoutError, RateLimitError, APIStatusError) as exc:
            if not self.fallback_client or (isinstance(exc, APIStatusError) and exc.status_code < 500 and exc.status_code != 429):
                raise
            logger.warning("Основная LLM недоступна (%s); переключаюсь на Gemini %s", type(exc).__name__, config.GEMINI_MODEL)
            fallback = (
                self.fallback_client.with_options(timeout=config.LLM_STARTUP_TIMEOUT_SECONDS)
                if startup else self.fallback_client
            )
            return await fallback.chat.completions.create(model=config.GEMINI_MODEL, **kwargs)

    async def verify_connection(self) -> bool:
        try:
            resp = await self._completion(
                startup=True,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5
            )
            logger.info("LLM API успешно ответил: model=%s", resp.model)
            return True
        except Exception as e:
            logger.error(f"Сбой подключения к LLM API ({self.base_url}): {e}")
            return False

    def _get_tools_spec(self) -> list:
        specs = [
            {
                "type": "function",
                "function": {
                    "name": "list_my_capabilities",
                "description": "Получить список возможностей Ады.",
                "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_dialogs",
                    "description": "Получить список последних диалогов Telegram.",
                    "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_chat_history",
                    "description": "Прочитать последние сообщения личного чата или группы.",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["target"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_telegram",
                    "description": "Найти сообщения в доступной истории Telegram.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_system_command",
                    "description": "Выполнить системную команду в PowerShell/CMD/Bash (только для Создателя).",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Прочитать текстовый файл с диска.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Записать файл на диск (только для Создателя).",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": "Посмотреть файлы и папки в каталоге.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_web_page",
                    "description": "Прочитать статью или текст по интернет-ссылке (URL).",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "update_my_profile",
                    "description": "Сменить имя или био в Telegram.",
                    "parameters": {
                        "type": "object",
                        "properties": {"first_name": {"type": "string"}, "about": {"type": "string"}}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "find_contact",
                    "description": "Найти человека в Telegram или базе данных.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "send_message_to_user",
                    "description": "Отправить сообщение пользователю или в группу.",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}, "message": {"type": "string"}},
                        "required": ["target", "message"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "Поиск по сохраненным воспоминаниям.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]
                    }
                }
            }
        ]
        def action(name, description, properties, required):
            return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}}}
        target_id = {"target": {"type": "string"}, "message_id": {"type": "integer"}}
        specs.extend([
            action("edit_message", "Отредактировать своё сообщение.", {**target_id, "new_text": {"type": "string"}}, ["target", "message_id", "new_text"]),
            action("delete_message", "Удалить сообщение.", {**target_id, "revoke": {"type": "boolean"}}, ["target", "message_id"]),
            action("reply_to_message", "Ответить на конкретное сообщение.", {**target_id, "message": {"type": "string"}}, ["target", "message_id", "message"]),
            action("react_to_message", "Поставить реакцию.", {**target_id, "reaction": {"type": "string"}}, ["target", "message_id"]),
            action("forward_message", "Переслать сообщение.", {**target_id, "destination": {"type": "string"}}, ["target", "message_id", "destination"]),
            action("pin_message", "Закрепить сообщение.", {**target_id, "notify": {"type": "boolean"}}, ["target", "message_id"]),
            action("mark_chat_read", "Отметить чат прочитанным.", {"target": {"type": "string"}}, ["target"]),
            action("get_group_members", "Получить участников группы.", {"target": {"type": "string"}, "limit": {"type": "integer"}}, ["target"]),
            action("get_chat_info", "Получить информацию о чате.", {"target": {"type": "string"}}, ["target"]),

            action("search_scoped_memory", "Искать в контекстной памяти.", {"query": {"type": "string"}, "owner_id": {"type": "integer"}, "chat_id": {"type": "integer"}}, ["query"]),
            action("create_reminder", "Создать напоминание в ISO-времени; repeat_rule — daily, weekly или weekdays для повторяющихся.", {"owner_id": {"type": "integer"}, "description": {"type": "string"}, "due_at": {"type": "string"}, "chat_id": {"type": "integer"}, "repeat_rule": {"type": "string"}}, ["owner_id", "description", "due_at"]),
            action("add_calendar_event", "Добавить событие в календарь владельца.", {"owner_id": {"type": "integer"}, "title": {"type": "string"}, "start_at": {"type": "string"}, "end_at": {"type": "string"}, "details": {"type": "string"}}, ["owner_id", "title", "start_at"]),
            action("list_calendar_events", "Показать события календаря.", {"from_at": {"type": "string"}, "to_at": {"type": "string"}}, []),
            action("import_schedule_json", "Импортировать расписание из JSON.", {"content": {"type": "string"}}, ["content"]),
            action("read_schedule", "Прочитать импортированное расписание.", {}, []),
            action("list_tasks", "Показать активные напоминания.", {"owner_id": {"type": "integer"}}, []),
            action("cancel_task", "Отменить напоминание по ID (только Создатель).", {"task_id": {"type": "integer"}}, ["task_id"]),
            action("get_college_schedule", "Расписание колледжа УКРТБ на дату: 'сегодня', 'завтра' или YYYY-MM-DD; query — название группы, преподавателя или аудитории (пусто — группа Создателя).", {"when": {"type": "string"}, "query": {"type": "string"}}, []),
            action("refresh_my_schedule_cache", "Обновить локальный кэш расписания группы УКРТБ с сайта.", {}, []),
            action("delete_calendar_event", "Удалить встречу по ID.", {"event_id": {"type": "integer"}}, ["event_id"]),
            action("create_meeting_request", "Сохранить запрос на встречу, не подтверждая её.", {"requester_id": {"type": "integer"}, "requester_name": {"type": "string"}, "purpose": {"type": "string"}, "preferred_time": {"type": "string"}}, ["requester_id", "requester_name", "purpose"]),
            action("list_meeting_requests", "Показать владельцу необработанные запросы на встречу.", {}, []),
            action("set_person_category", "Самостоятельно изменить категорию текущего собеседника по устойчивому поведению; нельзя назначать админа или менять Создателя.", {"user_id": {"type": "integer"}, "category": {"type": "string"}}, ["user_id", "category"]),
            action("set_person_respect", "Самостоятельно изменить уважение к текущему собеседнику (0..100) с учётом его поведения; Создатель неизменен.", {"user_id": {"type": "integer"}, "respect": {"type": "integer"}}, ["user_id", "respect"]),
            action("set_mood", "Самостоятельно изменить собственную эмоцию (0..100) в ответ на ситуацию.", {"emotion": {"type": "string"}, "value": {"type": "integer"}}, ["emotion", "value"]),
        ])
        return specs



    async def run_turn(self, system_prompt: str, user_prompt: str, max_steps: int = 5) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        actions_taken = []

        for _ in range(max_steps):
            try:
                response = await self._completion(
                    messages=messages,
                    tools=self._get_tools_spec(),
                    temperature=0.6
                )
            except Exception as e:
                logger.error(f"Ошибка LLM API: {e}")
                return {"reply": "Прости, возникла заминка с сетью.", "actions": actions_taken}

            choice = response.choices[0]
            msg = choice.message
            messages.append(msg)

            if not msg.tool_calls:
                reply = msg.content.strip() if msg.content else ""
                return {
                    "reply": reply,
                    "messages": self._split_reply(reply),
                    "actions": actions_taken
                }

            for tool_call in msg.tool_calls:
                fname = tool_call.function.name
                try:
                    fargs = json.loads(tool_call.function.arguments)
                except Exception:
                    fargs = {}

                actions_taken.append(f"{fname}({fargs})")

                if fname in self.tool_functions:
                    try:
                        res = await self.tool_functions[fname](**fargs)
                    except Exception as err:
                        res = f"Ошибка: {err}"
                else:
                    res = "Инструмент не найден"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(res)
                })

        fallback = "Я выполнила действия."
        return {"reply": fallback, "messages": [fallback], "actions": actions_taken}

    @staticmethod
    def _split_reply(text: str) -> list[str]:
        """Поддерживает явную выдачу нескольких отдельных Telegram-сообщений."""
        text = text.strip()
        if not text:
            return []
        if "<MESSAGE>" in text and "</MESSAGE>" in text:
            import re
            parts = [p.strip() for p in re.findall(r"<MESSAGE>(.*?)</MESSAGE>", text, re.S)]
            if parts:
                return parts[:6]
        return [text]

    async def summarize_dialog(self, text_chunk: str) -> str:
        try:
            resp = await self._completion(
                messages=[
                    {"role": "system", "content": "Выдели 2-3 ключевых факта из старого диалога. Только суть списком, без приветствий."},
                    {"role": "user", "content": text_chunk}
                ],
                max_tokens=250
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return ""

    async def compress_global_memories(self, memories_text: str) -> list:
        try:
            resp = await self._completion(
                messages=[
                    {"role": "system", "content": "Сожми воспоминания в JSON-массив: [{\"category\": \"тема\", \"content\": \"факт\"}]"},
                    {"role": "user", "content": memories_text}
                ]
            )
            raw = resp.choices[0].message.content.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            return json.loads(raw)
        except Exception:
            return []
