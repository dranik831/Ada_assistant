import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
MODULES_DIR = BASE_DIR / "modules"
ENV_FILE = BASE_DIR / ".env"
PERSONALITY_FILE = BASE_DIR / "personality.json"
PERSONA_LOCAL_FILE = DATA_DIR / "persona_local.txt"  # локальное дополнение к характеру, не коммитится

DEFAULT_ENV = """# Telegram API credentials (https://my.telegram.org)
TG_API_ID=
TG_API_HASH=
TG_PHONE=

# Администратор
ADMIN_USERNAME=dranik83
ADMIN_USER_ID=0

# Отдельный Telegram-бот управления (control_bot.py)
CONTROL_BOT_TOKEN=
CONTROL_BOT_ALLOWED_IDS=
CONTROL_WEB_URL=
CONTROL_WEB_PORT=8765
CONTROL_WEB_BIND=127.0.0.1
CONTROL_WEB_PROXY_PEER=
ADA_LOG_LEVEL=INFO

# Цепочка провайдеров LLM в порядке приоритета (через запятую). Первый
# отвечающий используется; при любой ошибке Ада переходит к следующему.
# Доступны: openai_compatible, gemini, anthropic.
LLM_PROVIDERS=openai_compatible,gemini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-5
# Без хвостового /v1 — Anthropic SDK сам добавляет /v1/messages к этому адресу.
ANTHROPIC_BASE_URL=https://api.anthropic.com
LLM_TIMEOUT_SECONDS=45
LLM_STARTUP_TIMEOUT_SECONDS=12

# Модули из папки modules/ (имя папки = имя флага). false отключает модуль целиком,
# вместе с его зависимостями: выключенный модуль не импортируется.
MODULE_VOICE=true
MODULE_VISION=false
MODULE_PC_CONTROL=true
MODULE_TELEGRAM_TOOLS=true
MODULE_SCHEDULE_COLLEGE=true
MODULE_SECRETARY=true
MODULE_EMAIL=false
MODULE_MINECRAFT=false

# Голосовое управление (Ctrl+Alt+Space). Модуль modules/voice
VOICE_ENABLED=true

# Minecraft (modules/minecraft): RCON-команды и чтение чата из logs/latest.log
MINECRAFT_RCON_HOST=127.0.0.1
MINECRAFT_RCON_PORT=25575
MINECRAFT_RCON_PASSWORD=
MINECRAFT_SERVER_DIR=
MINECRAFT_LOG_PATH=
MINECRAFT_CHAT_REPLY_PROBABILITY=0.3
MINECRAFT_CHAT_COOLDOWN_SECONDS=20

# Зрение (modules/vision): по умолчанию пустой VISION_BASE_URL значит "использовать
# основной LLM-провайдер (ACTIVE_LLM_*)". Заполните, чтобы взять отдельную
# vision-модель, например локальную VLM через Ollama (http://localhost:11434/v1).
VISION_BASE_URL=
VISION_MODEL=
VISION_API_KEY=
VISION_TIMEOUT_SECONDS=120

# Распознавание речи: local (Whisper medium на компе) или api (внешний сервер)
WHISPER_MODE=local
WHISPER_API_URL=
WHISPER_API_KEY=

# Настройки автономности
AUTONOMY_ENABLED=true
GROUP_AUTONOMY_ENABLED=true
GROUP_REPLY_PROBABILITY=0.08
GROUP_REPLY_COOLDOWN_MINUTES=20
MESSAGE_DEBOUNCE_SECONDS=3
CONTEXT_CHATS=3
CONTEXT_MESSAGES_PER_CHAT=10
CONTEXT_FORGET_MINUTES=20
SECRETARY_ENABLED=true
CALENDAR_FILE=data/calendar.json
SCHEDULE_FILE=data/schedule.json
MAINTENANCE_NAME=Ада | ТЕХ РАБОТЫ ОК ДА
NORMAL_NAME=Ада
AUTONOMY_CHECK_INTERVAL=300
AUTONOMY_SILENCE_MINUTES=45
QUIET_HOURS_START=23
QUIET_HOURS_END=8

# Расписание УКРТБ (study.ukrtb.ru)
UKRTB_GROUP_ID=
UKRTB_GROUP_LABEL=

# Почта (IMAP, только чтение). Для Gmail/Яндекс/Mail.ru нужен пароль приложения
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_USER=
EMAIL_PASSWORD=

# Белый список системных команд (через запятую, пусто = любые)
SYSTEM_COMMAND_ALLOWLIST=
"""

def init_environment():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if not ENV_FILE.exists():
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_ENV)
        print(f"[!] Создан шаблон конфигурации: {ENV_FILE}")
        print("[!] Заполните TG_API_ID, TG_API_HASH, TG_PHONE и GEMINI_API_KEY.")
        sys.exit(0)

init_environment()
load_dotenv(ENV_FILE)

TG_API_ID = os.getenv("TG_API_ID", "").strip()
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_PHONE = os.getenv("TG_PHONE", "").strip()

# Telegram proxy. The default is SOCKS5; MTProto requires a 32-character secret.
TG_PROXY_ENABLED = os.getenv("TG_PROXY_ENABLED", "false").lower() == "true"
TG_PROXY_TYPE = os.getenv("TG_PROXY_TYPE", "mtproxy").strip().lower()
TG_PROXY_HOST = os.getenv("TG_PROXY_HOST", "127.0.0.1").strip()
TG_PROXY_PORT = int(os.getenv("TG_PROXY_PORT", "1080"))
TG_PROXY_SECRET = os.getenv("TG_PROXY_SECRET", "").strip()

if TG_PROXY_ENABLED:
    if TG_PROXY_TYPE == "mtproxy":
        # MTProto is selected by Telethon's connection class, not by a
        # string protocol name inside the proxy tuple.
        TG_PROXY = (TG_PROXY_HOST, TG_PROXY_PORT, TG_PROXY_SECRET)
        TG_CONNECTION_MODE = "mtproxy"
    elif TG_PROXY_TYPE in {"socks5", "socks5h", "http"}:
        # Optional SOCKS/HTTP credentials are read from the same secret fields.
        TG_PROXY_USERNAME = os.getenv("TG_PROXY_USERNAME", "").strip()
        TG_PROXY = (TG_PROXY_TYPE, TG_PROXY_HOST, TG_PROXY_PORT, True, TG_PROXY_USERNAME or None, TG_PROXY_SECRET or None)
        TG_CONNECTION_MODE = "proxy"
    else:
        raise ValueError("TG_PROXY_TYPE должен быть mtproxy, socks5, socks5h или http")
else:
    TG_PROXY = None
    TG_CONNECTION_MODE = "direct"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "dranik83").replace("@", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# Устаревшая переменная одного провайдера; если задана без LLM_PROVIDERS,
# цепочка строится из неё одной (см. LLM_PROVIDER_CHAIN ниже).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip()
LLM_TIMEOUT_SECONDS = max(5, int(os.getenv("LLM_TIMEOUT_SECONDS", "45")))
LLM_STARTUP_TIMEOUT_SECONDS = max(3, int(os.getenv("LLM_STARTUP_TIMEOUT_SECONDS", "12")))

_KNOWN_LLM_PROVIDERS = {"gemini", "openai_compatible", "anthropic"}


def _provider_settings(kind: str) -> dict:
    """base_url/api_key/model конкретного типа провайдера."""
    if kind == "gemini":
        return {"kind": kind, "base_url": GEMINI_BASE_URL,
                # Backward compatibility: older installations stored the Gemini key in LLM_API_KEY.
                "api_key": GEMINI_API_KEY or LLM_API_KEY, "model": GEMINI_MODEL}
    if kind == "anthropic":
        return {"kind": kind, "base_url": ANTHROPIC_BASE_URL, "api_key": ANTHROPIC_API_KEY, "model": ANTHROPIC_MODEL}
    if kind == "openai_compatible":
        return {"kind": kind, "base_url": LLM_BASE_URL, "api_key": LLM_API_KEY, "model": LLM_MODEL}
    raise ValueError(f"Неизвестный тип провайдера LLM: {kind}")


def _build_provider_chain() -> list[dict]:
    raw = os.getenv("LLM_PROVIDERS", "").strip()
    if not raw and LLM_PROVIDER:
        # Обратная совместимость: старые инсталляции задавали один LLM_PROVIDER и
        # получали автоматический fallback на Gemini — сохраняем это поведение.
        raw = LLM_PROVIDER if LLM_PROVIDER == "gemini" else f"{LLM_PROVIDER},gemini"
    if not raw:
        raw = "openai_compatible,gemini"
    kinds = [item.strip().lower() for item in raw.split(",") if item.strip()]
    unknown = [kind for kind in kinds if kind not in _KNOWN_LLM_PROVIDERS]
    if unknown:
        raise ValueError(
            f"LLM_PROVIDERS содержит неизвестные провайдеры: {', '.join(unknown)}. "
            f"Доступны: {', '.join(sorted(_KNOWN_LLM_PROVIDERS))}."
        )
    chain = [_provider_settings(kind) for kind in kinds]
    # Провайдер без ключа бесполезен в цепочке — пропускаем его молча.
    return [item for item in chain if item["api_key"]]


# Цепочка провайдеров в порядке приоритета: [{"kind", "base_url", "api_key", "model"}, ...]
LLM_PROVIDER_CHAIN = _build_provider_chain()

# Обратная совместимость: "активный" провайдер — первый в цепочке (используется
# модулями, которым не нужна отказоустойчивость, например vision).
_primary = LLM_PROVIDER_CHAIN[0] if LLM_PROVIDER_CHAIN else _provider_settings("openai_compatible")
ACTIVE_LLM_BASE_URL = _primary["base_url"]
ACTIVE_LLM_API_KEY = _primary["api_key"]
ACTIVE_LLM_MODEL = _primary["model"]
# LLM_PROVIDER как имя первого провайдера цепочки — для логов и диагностики.
LLM_PROVIDER = _primary["kind"]

AUTONOMY_ENABLED = os.getenv("AUTONOMY_ENABLED", "true").lower() == "true"
GROUP_AUTONOMY_ENABLED = os.getenv("GROUP_AUTONOMY_ENABLED", "true").lower() == "true"
GROUP_REPLY_PROBABILITY = max(0.0, min(1.0, float(os.getenv("GROUP_REPLY_PROBABILITY", "0.08"))))
GROUP_REPLY_COOLDOWN_MINUTES = max(1, int(os.getenv("GROUP_REPLY_COOLDOWN_MINUTES", "20")))
MESSAGE_DEBOUNCE_SECONDS = max(0.5, float(os.getenv("MESSAGE_DEBOUNCE_SECONDS", "3")))
SECRETARY_ENABLED = os.getenv("SECRETARY_ENABLED", "true").lower() == "true"
CALENDAR_FILE = BASE_DIR / os.getenv("CALENDAR_FILE", "data/calendar.json")
SCHEDULE_FILE = BASE_DIR / os.getenv("SCHEDULE_FILE", "data/schedule.json")
MAINTENANCE_NAME = os.getenv("MAINTENANCE_NAME", "Ада | ТЕХ РАБОТЫ ОК ДА")
NORMAL_NAME = os.getenv("NORMAL_NAME", "Ада")
AUTONOMY_CHECK_INTERVAL = int(os.getenv("AUTONOMY_CHECK_INTERVAL", "300"))
AUTONOMY_SILENCE_MINUTES = int(os.getenv("AUTONOMY_SILENCE_MINUTES", "45"))
QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "23"))
QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "8"))

# Учебный портал УКРТБ (study.ukrtb.ru): расписание занятий.
UKRTB_GROUP_ID = os.getenv("UKRTB_GROUP_ID", "bf399ce2-089f-11f1-9b37-864d651ef868").strip()
EMAIL_IMAP_HOST = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com").strip()
EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
UKRTB_GROUP_LABEL = os.getenv("UKRTB_GROUP_LABEL", "ССА-17-26П").strip()

# Белый список системных команд (через запятую). Пусто = разрешены любые
# команды для Создателя; заполнено = только перечисленные программы.
SYSTEM_COMMAND_ALLOWLIST = [
    token.strip().lower()
    for token in os.getenv("SYSTEM_COMMAND_ALLOWLIST", "").split(",")
    if token.strip()
]

# Контекст диалога: сколько последних чатов (включая текущий) и сколько
# последних сообщений из каждого передаётся модели. Фоновый чат (не текущий),
# который молчал дольше CONTEXT_FORGET_MINUTES, в контекст не попадает.
CONTEXT_CHATS = max(1, int(os.getenv("CONTEXT_CHATS", "3")))
CONTEXT_MESSAGES_PER_CHAT = max(1, int(os.getenv("CONTEXT_MESSAGES_PER_CHAT", "10")))
CONTEXT_FORGET_MINUTES = max(1, int(os.getenv("CONTEXT_FORGET_MINUTES", "20")))

# Распознавание речи
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "true").lower() == "true"
# Пустой VISION_BASE_URL (по умолчанию) — модуль vision использует основной
# LLM-провайдер (ACTIVE_LLM_*) вместо отдельной vision-модели.
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "").strip()
VISION_MODEL = os.getenv("VISION_MODEL", "").strip()
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip()
VISION_TIMEOUT_SECONDS = max(5, int(os.getenv("VISION_TIMEOUT_SECONDS", "120")))
WHISPER_MODE = os.getenv("WHISPER_MODE", "local").strip().lower()
WHISPER_API_URL = os.getenv("WHISPER_API_URL", "").strip()
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "").strip()

DB_PATH = DATA_DIR / "ada.db"
SESSION_PATH = DATA_DIR / "ada_userbot"
