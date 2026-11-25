import json

import config


def make_editor(db, emotions, personality, modules, agent, tools, stop):
    async def module_tool(name, *args):
        func = modules.tool_functions().get(name)
        return await func(*args) if func else "Инструмент недоступен: модуль выключен или не загрузился."

    async def handle_admin_command(event, command: str, args: list) -> bool:
        cmd = command.lower()

        if cmd == "/status":
            state = await emotions.get_state()
            traits = personality.get_traits()
            auto = await db.get_kv("autonomy_enabled", "true" if config.AUTONOMY_ENABLED else "false")

            emo_lines = "\n".join([f"• {k}: {v}" for k, v in state.items()])
            trait_lines = "\n".join([f"• {k}: {v}" for k, v in traits.items()])

            text = (
                f"Состояние Ады:\n\n"
                f"**Эмоции:**\n{emo_lines}\n\n"
                f"**Черты:**\n{trait_lines}\n\n"
                f"**Автономность:** {'ВКЛ' if auto == 'true' else 'ВЫКЛ'}"
            )
            await event.reply(text)
            return True

        elif cmd == "/set_mood" and len(args) >= 2:
            try:
                await emotions.set_emotion(args[0].lower(), int(args[1]))
                await event.reply(f"Настроение обновлено: `{args[0]}` = {args[1]}")
            except ValueError:
                pass
            return True

        elif cmd == "/set_trait" and len(args) >= 2:
            try:
                if personality.set_trait(args[0].lower(), int(args[1])):
                    await event.reply(f"Черта обновлена: `{args[0]}` = {args[1]}")
            except ValueError:
                pass
            return True

        elif cmd == "/autonomy" and len(args) >= 1:
            state = "true" if args[0].lower() == "on" else "false"
            await db.set_kv("autonomy_enabled", state)
            await event.reply(f"Автономность: {'ВКЛ' if state == 'true' else 'ВЫКЛ'}")
            return True

        elif cmd == "/memory":
            mems = await db.get_all_memories()
            if not mems:
                await event.reply("В долгосрочной памяти пока пусто.")
            else:
                lines = [f"- [{m['category']}] {m['content']}" for m in mems[-8:]]
                await event.reply("**Воспоминания:**\n" + "\n".join(lines))
            return True

        return False


    async def edit_menu(event) -> None:
        await event.reply(
            "🛠 Режим редактирования включён. ИИ не вызывается.\n\n"
            "Команды:\n"
            "• эмоции — показать эмоции\n"
            "• эмоции <название> <0..100> — изменить эмоцию\n"
            "• состояние — показать аккаунт и текущие настройки\n"
            "• модули — список модулей и их состояние\n"
            "• память — показать долговременную память\n"
            "• память добавить личное|global <текст> — добавить факт\n"
            "• память добавить группа <chat_id> <текст> — добавить факт группы\n"

            "• память удалить <id> — удалить факт\n"
            "• память история — журнал изменений фактов\n"
            "• память очистить — очистить историю и память\n"
            "• правило <chat_id> <текст> — локальное правило группы\n"
            "• уважение <id> <0..100> — изменить уважение\n"
            "• категория <id> стандарт|друг|админ — изменить категорию\n"
            "• роль добавить <id> стандарт|друг — назначить роль (админа только владелец в конфиге)\n"
            "• календарь добавить <ISO-дата-время> <название> — добавить событие\n"
            "• календарь — список событий\n"
            "• календарь удалить <id> — удалить встречу\n"
            "• расписание импорт <JSON> — загрузить расписание\n"
            "• расписание показать — посмотреть загруженное расписание\n"
            "• расписание сегодня|завтра — расписание УКРТБ с сайта\n"
            "• расписание обновить — обновить кэш расписания УКРТБ\n"
            "• задача отменить <id> — отменить напоминание\n"
            "• задачи — список напоминаний\n"
            "• запросы — запросы на встречи\n"
            "• техработы — включить служебное имя и остановить\n"
            "• /restart — перезапустить Аду\n"
            "• промпт — показать системный промпт\n"

            "• промпт установить <текст> — заменить базовый промпт\n"
            "• имя <текст> — изменить имя Telegram-профиля\n"
            "• био <текст> — изменить био Telegram-профиля\n"
            "• выйти — вернуться в обычный режим"
        )

    async def handle_edit_mode(event, text: str) -> bool:
        tools.set_request_context(event.sender_id, event.sender_id, True, True)
        parts = text.split(maxsplit=3)
        command = parts[0].lower() if parts else ""

        if command in {"помощь", "меню", "?"}:
            await edit_menu(event)
            return True
        if command == "эмоции":
            if len(parts) == 1:
                await event.reply(json.dumps(await emotions.get_state(), ensure_ascii=False, indent=2))
            elif len(parts) == 3:
                try:
                    await emotions.set_emotion(parts[1].lower(), int(parts[2]))
                    await event.reply(f"Эмоция изменена: {parts[1]} = {parts[2]}")
                except (ValueError, TypeError):
                    await event.reply("Формат: эмоции energy 80")
            else:
                await event.reply("Формат: эмоции <название> <0..100>")
            return True
        if command == "состояние":
            active_modules = modules.active_names()
            provider_chain = " -> ".join(str(p) for p in agent.providers)
            await event.reply(
                f"Провайдеры LLM (по приоритету): {provider_chain}\n"
                f"Автономность: {await db.get_kv('autonomy_enabled', 'true')}\n"
                f"Модули: {', '.join(active_modules) if active_modules else 'нет активных'}\n"
                f"Эмоции: {json.dumps(await emotions.get_state(), ensure_ascii=False)}"
            )
            return True
        if command == "модули":
            await event.reply(
                modules.status_report()[:3900]
                + "\n\nВключение и отключение — в .env (MODULE_<ИМЯ>=true/false), затем «перезапуск»."
            )
            return True
        if command == "техработы":
            await event.reply("Включаю режим технических работ и останавливаюсь.")
            stop.set()
            return True
        if command == "уважение" and len(parts) == 3:
            try:
                await db.update_user_relationship(int(parts[1]), {"respect": max(0, min(100, int(parts[2])))})
                await event.reply("Уровень уважения обновлён.")
            except ValueError:
                await event.reply("Формат: уважение <id> <0..100>")
            return True
        if command == "категория" and len(parts) == 3:
            category = parts[2].lower()
            target_id = int(parts[1])
            if category in {"стандарт", "друг", "админ"}:
                await db.set_user_category(target_id, category)
                await event.reply(f"Категория пользователя изменена: {category}.")
            else:
                await event.reply("Категория: стандарт, друг или админ.")
            return True
        if command == "роль" and len(parts) == 4 and parts[1].lower() == "добавить":
            category = parts[3].lower()
            if category not in {"стандарт", "друг"}:
                await event.reply("Команда может назначить только стандарт или друг.")
            else:
                await db.set_user_category(int(parts[2]), category)
                await event.reply(f"Категория назначена: {category}.")
            return True
        if command == "эмоция" and len(parts) == 3:
            try:
                await emotions.set_emotion(parts[1].lower(), int(parts[2]))
                await event.reply("Эмоциональное состояние обновлено.")
            except ValueError:
                await event.reply("Формат: эмоция happiness 70")
            return True
        if command == "календарь":
            if len(parts) == 1:
                await event.reply(await module_tool("list_calendar_events"))
            elif len(parts) >= 4 and parts[1].lower() == "добавить":
                title = parts[3]
                await event.reply(await module_tool("add_calendar_event", config.ADMIN_USER_ID, title, parts[2]))
            elif len(parts) == 3 and parts[1].lower() == "удалить":
                try:
                    await event.reply(await module_tool("delete_calendar_event", int(parts[2])))
                except ValueError:
                    await event.reply("Формат: календарь удалить <id>")
            else:
                await event.reply("Формат: календарь добавить <ISO-дата-время> <название>")
            return True
        if command == "расписание" and len(parts) >= 2 and parts[1].lower() == "показать":
            await event.reply(await module_tool("read_schedule"))
            return True
        if command == "расписание" and len(parts) >= 3 and parts[1].lower() == "импорт":
            await event.reply(await module_tool("import_schedule_json", text.split(maxsplit=2)[2]))
            return True
        if command == "расписание" and len(parts) >= 2 and parts[1].lower() == "обновить":
            await event.reply(await module_tool("refresh_my_schedule_cache"))
            return True
        if command == "расписание" and len(parts) >= 2 and parts[1].lower() in {"сегодня", "завтра", "укртб"}:
            when = "сегодня" if parts[1].lower() == "сегодня" else ("завтра" if parts[1].lower() == "завтра" else "")
            await event.reply(await module_tool("get_college_schedule", when))
            return True
        if command == "задача" and len(parts) == 3 and parts[1].lower() == "отменить":
            try:
                await event.reply(await module_tool("cancel_task", int(parts[2])))
            except ValueError:
                await event.reply("Формат: задача отменить <id>")
            return True
        if command == "задачи":
            await event.reply(await module_tool("list_tasks", config.ADMIN_USER_ID))
            return True
        if command == "запросы":
            await event.reply(await module_tool("list_meeting_requests"))
            return True
        if command == "правило":
            if len(parts) >= 3 and parts[1].lstrip("-").isdigit():
                await db.update_chat_context(int(parts[1]), rules=text.split(maxsplit=2)[2])
                await event.reply(f"Правило чата {parts[1]} сохранено.")
            elif len(parts) == 3 and parts[1].lower() == "показать" and parts[2].lstrip("-").isdigit():
                chat_row = await db.get_chat(int(parts[2]))
                await event.reply(f"Правила чата {parts[2]}:\n{(chat_row or {}).get('rules') or 'не заданы'}")
            else:
                await event.reply("Формат: правило <chat_id> <текст правила>")
            return True
        if command == "память" and len(parts) == 2 and parts[1].lower() == "история":
            history = await db.get_memory_history(limit=25)
            text_out = "\n".join(
                f"#{h['memory_id']} [{h['action']}] {h['content'][:120]}" for h in history
            )
            await event.reply(text_out[:3900] or "История памяти пуста.")
            return True
        if command == "память" and len(parts) == 2 and parts[1].lower() == "очистить":
            await db.clear_memory()
            await event.reply("Память, история и журналы очищены.")
            return True
        if command == "память":
            if len(parts) == 1:
                memories = await db.list_scoped_memories(60)
                text_out = "\n".join(
                    f"#{m['id']} [{m['scope']}/{m['category']}; {m['confidence']:.2f}] {m['content']}"
                    for m in memories
                )
                await event.reply(text_out[:3900] or "Память пуста.")

            elif len(parts) == 4 and parts[1].lower() == "добавить":
                scope = parts[2].lower()
                fact, group_id = parts[3], None
                if scope == "группа":
                    group = fact.split(maxsplit=1)
                    if len(group) != 2 or not group[0].startswith("-") or not group[0][1:].isdigit():
                        await event.reply("Формат: память добавить группа <chat_id> <текст>.")
                        return True
                    group_id, fact = int(group[0]), group[1]
                if scope not in {"личное", "группа", "global"}:
                    await event.reply("Область памяти: личное, группа или global.")
                else:
                    await db.add_scoped_memory(
                        {"личное": "person", "группа": "group", "global": "global"}[scope],
                        fact,
                        category="explicit_fact",
                        owner_id=config.ADMIN_USER_ID if scope == "личное" else None,
                        chat_id=group_id,
                        importance=8,
                        confidence=1.0,
                        source="owner_edit_mode",
                    )
                    await event.reply("Подтверждённый факт добавлен.")
            elif len(parts) == 3 and parts[1].lower() == "удалить":
                try:
                    await db.delete_memory(int(parts[2]))
                    await event.reply("Факт удалён.")

                except ValueError:
                    await event.reply("Формат: память удалить <id>")
            else:
                await event.reply("Формат: память добавить <категория> <текст>")
            return True
        if command == "промпт":
            if len(parts) == 1:
                await event.reply(personality.get_prompt()[:3900])
            elif len(parts) >= 3 and parts[1].lower() == "установить":
                personality.set_prompt(text.split(maxsplit=2)[2])
                await event.reply("Базовый промпт обновлён.")
            return True
        if command in {"имя", "био"} and len(parts) >= 2:
            value = text.partition(" ")[2].strip()
            result = await tools.update_my_profile(first_name=value if command == "имя" else "", about=value if command == "био" else "")
            await event.reply(result)
            return True
        await event.reply("Неизвестная команда. Напишите «меню» или «выйти».")
        return True

    async def edit(text, actor):
        class Response:
            sender_id = actor
            replies = []

            async def reply(self, value):
                self.replies.append(value)

        event = Response()
        try:
            if text.startswith("/"):
                command, *args = text.split()
                if await handle_admin_command(event, command, args):
                    return "\n".join(event.replies) or "Проверь параметры команды."
            await handle_edit_mode(event, text)
        except ValueError:
            return "Проверь формат команды и числовые параметры."
        return "\n".join(event.replies) or "Проверь параметры команды."

    return edit
