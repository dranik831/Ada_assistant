import json
import logging
import re

from openai import AsyncOpenAI
from anthropic import AsyncAnthropic

from tools import AgentTools
import config
import module_system

logger = logging.getLogger("Ada.LLM")


class NormalizedMessage:
    """Единый формат ответа модели независимо от провайдера.

    content — текст ответа (может быть пустым, если модель только звала инструменты);
    tool_calls — [{"id", "name", "arguments": dict}, ...]."""

    __slots__ = ("content", "tool_calls")

    def __init__(self, content: str, tool_calls: list):
        self.content = content
        self.tool_calls = tool_calls


class _Provider:
    """Один провайдер цепочки: хранит клиент и знает, как с ним говорить."""

    def __init__(self, settings: dict):
        self.kind = settings["kind"]
        self.model = settings["model"]
        self.base_url = settings["base_url"]
        self.api_key = settings["api_key"]
        if self.kind == "anthropic":
            self.client = AsyncAnthropic(base_url=self.base_url or None, api_key=self.api_key,
                                         timeout=config.LLM_TIMEOUT_SECONDS, max_retries=0)
        else:
            default_headers = {}
            if "openrouter.ai" in self.base_url:
                default_headers = {
                    "HTTP-Referer": "https://github.com/dranik83/ada_tg",
                    "X-Title": "Ada Telegram Agent",
                }
            self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key,
                                      default_headers=default_headers,
                                      timeout=config.LLM_TIMEOUT_SECONDS, max_retries=0)

    def __repr__(self) -> str:
        return f"{self.kind}:{self.model}"

    async def complete(self, *, messages, tools=None, temperature=0.6, max_tokens=None,
                       timeout_override=None) -> NormalizedMessage:
        if self.kind == "anthropic":
            return await self._complete_anthropic(messages, tools, temperature, max_tokens, timeout_override)
        return await self._complete_openai(messages, tools, temperature, max_tokens, timeout_override)

    async def _complete_openai(self, messages, tools, temperature, max_tokens, timeout_override) -> NormalizedMessage:
        client = self.client.with_options(timeout=timeout_override) if timeout_override else self.client
        kwargs = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = []
        for call in (msg.tool_calls or []):
            try:
                args = json.loads(call.function.arguments)
            except Exception:
                args = {}
            tool_calls.append({"id": call.id, "name": call.function.name, "arguments": args})
        return NormalizedMessage(content=(msg.content or "").strip(), tool_calls=tool_calls)

    async def _complete_anthropic(self, messages, tools, temperature, max_tokens, timeout_override) -> NormalizedMessage:
        client = self.client.with_options(timeout=timeout_override) if timeout_override else self.client
        system_prompt, anthro_messages = _to_anthropic_messages(messages)
        kwargs = {
            "model": self.model,
            "messages": anthro_messages,
            "max_tokens": max_tokens or 4096,
            # В anthropic>=1.0 temperature/top_p/top_k больше не параметры метода
            # напрямую — передаются через extra_body.
            "extra_body": {"temperature": temperature},
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = [_openai_tool_to_anthropic(spec) for spec in tools]
        resp = await client.messages.create(**kwargs)
        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input or {}})
        return NormalizedMessage(content="\n".join(text_parts).strip(), tool_calls=tool_calls)


def _openai_tool_to_anthropic(spec: dict) -> dict:
    fn = spec["function"]
    return {"name": fn["name"], "description": fn.get("description", ""), "input_schema": fn.get("parameters", {})}


def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Конвертирует историю в формате OpenAI (system/user/assistant/tool) в
    Anthropic messages + system prompt. Используется только когда провайдер — anthropic."""
    system_parts = []
    out = []
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id") if isinstance(msg, dict) else msg.tool_call_id
            content = msg.get("content") if isinstance(msg, dict) else msg.content
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_call_id, "content": str(content)}
            ]})
        else:  # assistant (dict или объект NormalizedMessage/OpenAI message)
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "") or ""
            tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in (tool_calls or []):
                call_id = call.get("id") if isinstance(call, dict) else call.id
                name = call.get("name") if isinstance(call, dict) else call.name
                args = call.get("arguments") if isinstance(call, dict) else call.arguments
                blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
    return "\n\n".join(system_parts), out


class UniversalAgent:
    def __init__(self, tools_instance: AgentTools):
        self.tools = tools_instance

        if not config.LLM_PROVIDER_CHAIN:
            raise ValueError(
                "Нет ни одного настроенного провайдера LLM. Заполните LLM_PROVIDERS и ключи в .env."
            )
        self.providers = [_Provider(settings) for settings in config.LLM_PROVIDER_CHAIN]

        # Обратная совместимость с местами, которые читают provider/base_url/model напрямую.
        self.provider = self.providers[0].kind
        self.base_url = self.providers[0].base_url
        self.model_name = self.providers[0].model

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

    async def _completion(self, *, startup=False, messages, tools=None, temperature=0.6,
                          max_tokens=None) -> NormalizedMessage:
        """Пробует провайдеров по очереди в порядке приоритета; любая ошибка — к следующему."""
        timeout_override = config.LLM_STARTUP_TIMEOUT_SECONDS if startup else None
        last_exc = None
        for i, provider in enumerate(self.providers):
            try:
                return await provider.complete(
                    messages=messages, tools=tools, temperature=temperature,
                    max_tokens=max_tokens, timeout_override=timeout_override,
                )
            except Exception as exc:
                last_exc = exc
                if i + 1 < len(self.providers):
                    logger.warning("Провайдер %s недоступен (%s: %s); переключаюсь на %s",
                                   provider, type(exc).__name__, exc, self.providers[i + 1])
                continue
        raise last_exc

    async def verify_connection(self) -> bool:
        try:
            resp = await self._completion(startup=True, messages=[{"role": "user", "content": "ping"}], max_tokens=5)
            logger.info("LLM API успешно ответил (provider=%s)", self.providers[0])
            return True
        except Exception as e:
            logger.error(f"Сбой подключения ко всем LLM-провайдерам ({[str(p) for p in self.providers]}): {e}")
            return False

    def _base_tools_spec(self) -> list:
        """Базовый набор на каждый ход: ядро + telegram_tools + list_module_tools.

        Инструменты остальных модулей (ПК, секретарь, расписание, почта, зрение)
        не передаются сразу — модель сначала вызывает list_module_tools(module_name),
        получает их описание, и дальше в этом же run_turn может вызвать их по имени.
        После ответа набор возвращается к базовому.
        """
        def action(name, description, properties, required):
            return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}}}

        specs = [
            action("list_my_capabilities", "Получить список возможностей Ады.", {}, []),
            action("list_module_tools", "Узнать инструменты подключённого модуля (ПК, секретарь, расписание, "
                   "почта, зрение и т.п.) и сразу получить право их вызывать. Сначала посмотри список модулей "
                   "в list_my_capabilities, если не помнишь точное имя модуля.",
                   {"module_name": {"type": "string"}}, ["module_name"]),
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
        # telegram_tools доступен напрямую: это основной рабочий инструмент Ады в каждом чате.
        specs.extend(module_system.registry().tool_specs_for("telegram_tools"))
        return specs

    async def run_turn(self, system_prompt: str, user_prompt: str, max_steps: int = 6) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        actions_taken = []
        # Спеки модулей, раскрытых вызовом list_module_tools в рамках этого run_turn.
        # Живут только до конца текущего ответа — следующее сообщение начинается заново.
        unlocked_specs: list[dict] = []

        for _ in range(max_steps):
            try:
                msg = await self._completion(
                    messages=messages,
                    tools=self._base_tools_spec() + unlocked_specs,
                    temperature=0.6,
                )
            except Exception as e:
                logger.error(f"Ошибка LLM API: {e}")
                # Текст ошибки не уходит в чат и не сохраняется в историю:
                # вызывающий код сам решает, кого уведомить.
                return {"must_reply": False, "reply": "", "messages": [], "actions": actions_taken,
                        "error": f"{type(e).__name__}: {e}"}

            messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})

            if not msg.tool_calls:
                must_reply, parts = self._parse_reply(msg.content)
                return {
                    "must_reply": must_reply,
                    "reply": "\n".join(parts),
                    "messages": parts,
                    "actions": actions_taken,
                }

            for call in msg.tool_calls:
                fname = call["name"]
                fargs = call["arguments"] or {}

                actions_taken.append(f"{fname}({fargs})")

                if fname == "list_module_tools":
                    summary, new_specs = module_system.list_module_tools(fargs.get("module_name", ""))
                    known = {spec["function"]["name"] for spec in unlocked_specs}
                    unlocked_specs.extend(spec for spec in new_specs if spec["function"]["name"] not in known)
                    res = summary
                elif fname in self.tool_functions:
                    try:
                        res = await self.tool_functions[fname](**fargs)
                    except Exception as err:
                        res = f"Ошибка: {err}"
                else:
                    res = "Инструмент не найден"

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": str(res),
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
                    {"role": "user", "content": text_chunk},
                ],
                max_tokens=250,
            )
            return resp.content
        except Exception:
            return ""

    async def compress_global_memories(self, memories_text: str) -> list:
        try:
            resp = await self._completion(
                messages=[
                    {"role": "system", "content": "Сожми воспоминания в JSON-массив: [{\"category\": \"тема\", \"content\": \"факт\"}]"},
                    {"role": "user", "content": memories_text},
                ],
                max_tokens=2000,
            )
            raw = resp.content
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            return json.loads(raw)
        except Exception:
            return []
