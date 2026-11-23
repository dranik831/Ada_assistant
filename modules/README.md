# Модули Ады

Папка `modules/` — место для подключаемых возможностей. Модуль можно включить или
выключить в `.env`, не трогая код ядра. Выключенный модуль не импортируется, поэтому
его зависимости (например `torch`, `whisper`, `Pillow`) не нужны, пока он выключен.

## Как включить и выключить

Имя папки модуля задаёт имя флага:

```env
MODULE_VISION=true
MODULE_VOICE=false
```

Порядок поиска флага: `MODULE_<ИМЯ>`, затем устаревшее `<ИМЯ>_ENABLED`, затем `<ИМЯ>`.
Если в `.env` нет ни одного из них, берётся `DEFAULT_ENABLED` самого модуля.

Состояние модулей видно в режиме редактирования:

```text
редактирование 8338
модули
```

После правки `.env` нужен перезапуск (команда `перезапуск` в режиме редактирования).

## Как добавить свой модуль

1. Создайте `modules/<имя>/__init__.py` (или одиночный файл `modules/<имя>.py`).
2. Опишите модуль и реализуйте `setup(ctx)`.
3. Добавьте `MODULE_<ИМЯ>=true` в `.env` и `.env.example`.
4. Перезапустите Аду и проверьте командой `модули`.

Готовый образец для копирования: `modules/_template.py` (имена с подчёркиванием
Ада не загружает, поэтому сам шаблон не подключается).

Минимальный модуль:

```python
from module_system import ModuleContext

DESCRIPTION = "что умеет модуль"
VERSION = "1.0"
DEFAULT_ENABLED = True   # значение, когда в .env флага нет


def setup(ctx: ModuleContext) -> None:
    async def weather(city: str) -> str:
        return f"В городе {city} сейчас ясно."

    ctx.register_tool(
        "weather",
        weather,
        "Узнать погоду в городе.",
        {"city": {"type": "string"}},
        ["city"],
    )
    ctx.register_capability("weather: узнать погоду в городе")
```

## Что даёт `ctx`

| Поле | Что это |
| --- | --- |
| `ctx.name` | имя модуля |
| `ctx.db` | база данных Ады (`Database`) |
| `ctx.client` | Telegram-клиент Telethon; на момент `setup()` создан, но ещё не подключён |
| `ctx.tools` | `AgentTools`: проверки прав (`_is_admin()`, `_pc_allowed()`) и действия в Telegram |
| `ctx.emotions` | система эмоций |
| `ctx.config` | модуль `config` со всеми настройками `.env` |
| `ctx.logger` | логгер `Ada.Module.<имя>` |
| `ctx.service(key)` | произвольный сервис из ядра |

`ctx.service("brain")` — особый случай: `Brain` создаётся в `main.py` уже после
подключения всех модулей (он зависит от `UniversalAgent`, который собирает их
инструменты), поэтому в момент `setup()` его ещё нет. Вызывайте `ctx.service("brain")`
лениво — внутри хука или функции инструмента, которая выполнится позже, а не прямо
в `setup()`. К этому моменту `main.py` уже зарегистрирует его через
`modules.set_service("brain", brain)`. Так же можно добавлять и другие сервисы,
которых не было на момент `load_modules()`.

Методы регистрации:

- `ctx.register_tool(name, func, description, properties, required)` — инструмент для модели.
  `func` должен быть `async` и возвращать строку. `properties` и `required` — JSON Schema
  параметров, как в остальных инструментах Ады.
- `ctx.register_hook(hook, func)` — подписка на точку расширения.
- `ctx.register_capability(text)` — строка для `list_my_capabilities`.

## Точки расширения (хуки)

Константы объявлены в `module_system.py`:

| Константа | Подпись | Когда вызывается |
| --- | --- | --- |
| `HOOK_STARTUP` | `async () -> None` | после подключения к Telegram |
| `HOOK_SHUTDOWN` | `async () -> None` | при остановке Ады |
| `HOOK_MEDIA_LABEL` | `async (message) -> str` | нужна текстовая пометка к медиа, например `[фото: ...]` |
| `HOOK_MESSAGE_CONTEXT` | `async (message) -> str` | дополнительный контекст сообщения для промпта |
| `HOOK_TRANSCRIBE` | `async (bytes, suffix) -> str` | речь в текст |
| `HOOK_AUDIO_RECORDER` | `() -> recorder` | диктофон с `start/stop/cancel` и полем `active` |
| `HOOK_AUTONOMY_TICK` | `async (autonomy: AutonomyEngine) -> None` | каждый цикл автономности (``AUTONOMY_CHECK_INTERVAL``) |
| `HOOK_LOCAL_MENU` | `(on_submit, transcribe) -> bool` | запускает локальное меню ПК (Ctrl+Alt+A) |

`HOOK_MEDIA_LABEL`, `HOOK_TRANSCRIBE`, `HOOK_AUDIO_RECORDER` и `HOOK_LOCAL_MENU` берут
результат первого модуля, который вернул непустое значение. `HOOK_STARTUP`,
`HOOK_SHUTDOWN`, `HOOK_MESSAGE_CONTEXT` и `HOOK_AUTONOMY_TICK` вызывают все подписанные
модули. `autonomy` в `HOOK_AUTONOMY_TICK` — это `AutonomyEngine` (`autonomy.py`): даёт
`autonomy.db`, `autonomy.brain`, `autonomy.send_message(user_id, text)` и
`autonomy.is_quiet_time()` для периодических задач вроде напоминаний или проверки почты.

## Правила

- Ошибка в модуле не останавливает Аду: она попадает в лог и в вывод команды `модули`.
- Тяжёлые зависимости импортируйте внутри функций или в подфайлах модуля, чтобы
  выключенный модуль ничего не тянул.
- Права проверяйте через `ctx.tools`: `_is_admin()` для действий владельца,
  `_pc_allowed()` для всего, что касается компьютера Создателя.
- Имена инструментов уникальны. Дубликат игнорируется с предупреждением в логе.

## Текущие модули

| Модуль | Флаг | Что даёт |
| --- | --- | --- |
| `vision` | `MODULE_VISION` | описание фото и стикеров, инструмент `look_at_screen` |
| `voice` | `MODULE_VOICE` | запись с микрофона и распознавание речи для локального меню |
| `pc_control` | `MODULE_PC_CONTROL` | терминал, файлы, буфер обмена, открытие программ, локальное меню Ctrl+Alt+A |
| `telegram_tools` | `MODULE_TELEGRAM_TOOLS` | диалоги, история, поиск, редактирование/удаление/реакции/пересылка/закрепление сообщений |
| `schedule_college` | `MODULE_SCHEDULE_COLLEGE` | расписание колледжа УКРТБ и утренняя сводка Создателю |
| `secretary` | `MODULE_SECRETARY` | личный календарь, импортированное расписание, напоминания, запросы на встречу |
| `email` | `MODULE_EMAIL` | чтение почты Создателя по IMAP и уведомления о важных письмах |
| `minecraft` | `MODULE_MINECRAFT` | RCON-команды на сервере Minecraft и автономное общение в игровом чате (читает `logs/latest.log`) |

В ядре (`tools.py`) остаются: `fetch_web_page`, `send_message_to_user`, `send_file`,
`update_my_profile`, память (`search_memory`, `search_scoped_memory`) и управление
отношениями/настроением (`set_person_category`, `set_person_respect`, `set_mood`) —
они тесно связаны с `brain.py`/`main.py` и используются почти всегда.
