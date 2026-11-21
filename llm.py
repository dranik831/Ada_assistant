import os
import json
import logging
import re
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError, RateLimitError
from tools import AgentTools
import config
import module_system

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
            "fetch_web_page": self.tools.fetch_web_page,
            "update_my_profile": self.tools.update_my_profile,
            "send_message_to_user": self.tools.send_message_to_user,
            "send_file": self.tools.send_file,
            "search_memory": self.tools.search_memory,
            "search_scoped_memory": self.tools.search_scoped_memory,
            "set_person_category": self.tools.set_person_category,
            "set_person_respect": self.tools.set_person_respect,
            "set_mood": self.tools.set_mood,
        }
        # Инструменты подключённых модулей добавляются к встроенным: туда входят
        # все Telegram/ПК/секретарские/почтовые/расписные инструменты.
        self.tool_functions.update(module_system.registry().tool_functions())


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
        def action(name, description, properties, required):
            return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}}}

        specs = [
            action("list_my_capabilities", "Получить список возможностей Ады.", {}, []),
            action("fetch_web_page", "Прочитать статью или текст по интернет-ссылке (URL).",
                   {"url": {"type": "string"}}, ["url"]),
            action("update_my_profile", "Сменить имя или био в Telegram.",
                   {"first_name": {"type": "string"}, "about": {"type": "string"}}, []),
            action("send_message_to_user", "Отправить сообщение пользователю или в группу.",
                   {"target": {"type": "string"}, "message": {"type": "string"}}, ["target", "message"]),
            action("send_file", "Отправить файл или фото с ПК Создателя в чат (target — id или юзернейм), "
                   "caption — подпись. Только Создатель.",
                   {"target": {"type": "string"}, "path": {"type": "string"}, "caption": {"type": "string"}},
                   ["target", "path"]),
            action("search_memory", "Поиск по сохраненным воспоминаниям.",
                   {"query": {"type": "string"}}, ["query"]),
            action("search_scoped_memory", "Искать в контекстной памяти.",
                   {"query": {"type": "string"}, "owner_id": {"type": "integer"}, "chat_id": {"type": "integer"}},
                   ["query"]),
            action("set_person_category", "Самостоятельно изменить категорию текущего собеседника по устойчивому "
                   "поведению; нельзя назначать админа или менять Создателя.",
                   {"user_id": {"type": "integer"}, "category": {"type": "string"}}, ["user_id", "category"]),
            action("set_person_respect", "Самостоятельно изменить уважение к текущему собеседнику (0..100) с "
                   "учётом его поведения; Создатель неизменен.",
                   {"user_id": {"type": "integer"}, "respect": {"type": "integer"}}, ["user_id", "respect"]),
            action("set_mood", "Самостоятельно изменить собственную эмоцию (0..100) в ответ на ситуацию.",
                   {"emotion": {"type": "string"}, "value": {"type": "integer"}}, ["emotion", "value"]),
        ]
        # Остальные инструменты (Telegram, ПК, секретарь, почта, расписание, зрение, голос)
        # приходят из подключённых модулей — см. modules/README.md.
        specs.extend(module_system.registry().tool_specs())
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
                # Текст ошибки не уходит в чат и не сохраняется в историю:
                # вызывающий код сам решает, кого уведомить.
                return {"must_reply": False, "reply": "", "messages": [], "actions": actions_taken,
                        "error": f"{type(e).__name__}: {e}"}

            choice = response.choices[0]
            msg = choice.message
            messages.append(msg)

            if not msg.tool_calls:
                raw = msg.content.strip() if msg.content else ""
                must_reply, parts = self._parse_reply(raw)
                return {
                    "must_reply": must_reply,
                    "reply": "\n".join(parts),
                    "messages": parts,
                    "actions": actions_taken,
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
        return {"must_reply": True, "reply": fallback, "messages": [fallback], "actions": actions_taken}

    @staticmethod
    def _parse_reply(text: str) -> tuple[bool, list[str]]:
        """Разбирает финальный ответ {"must_reply": bool, "messages": [...]}.

        Если модель ответила не JSON, текст считается обычным ответом (старый формат)."""
        text = (text or "").strip()
        if not text:
            return False, []
        candidate = text
        if "```" in candidate:
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate.strip(), flags=re.I)
        start, end = candidate.find("{"), candidate.rfind("}")
        data = None
        if start != -1 and end > start:
            try:
                data = json.loads(candidate[start:end + 1], strict=False)
            except ValueError:
                data = None
        if not isinstance(data, dict) or not ({"must_reply", "messages"} & data.keys()):
            parts = UniversalAgent._split_reply(text)
            return bool(parts), parts

        must_reply = data.get("must_reply", True)
        if isinstance(must_reply, str):
            must_reply = must_reply.strip().lower() not in {"false", "0", "no", "нет"}
        raw_messages = data.get("messages", data.get("message", data.get("reply", [])))
        if isinstance(raw_messages, str):
            raw_messages = [raw_messages]
        parts = [str(part).strip() for part in raw_messages or [] if str(part).strip()]
        if not must_reply or not parts:
            return False, []
        return True, parts[:6]

    @staticmethod
    def _split_reply(text: str) -> list[str]:
        """Поддерживает явную выдачу нескольких отдельных Telegram-сообщений."""
        text = text.strip()
        if not text:
            return []
        if "<MESSAGE>" in text and "</MESSAGE>" in text:
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
