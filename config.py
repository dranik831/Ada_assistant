import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
ENV_FILE = BASE_DIR / ".env"
PERSONALITY_FILE = BASE_DIR / "personality.json"

DEFAULT_ENV = """# Telegram API credentials (https://my.telegram.org)
TG_API_ID=
TG_API_HASH=
TG_PHONE=

# Администратор
ADMIN_USERNAME=dranik83
ADMIN_USER_ID=0

# LLM provider: gemini (default) or openai_compatible
LLM_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT_SECONDS=45
LLM_STARTUP_TIMEOUT_SECONDS=12

# Настройки автономности
AUTONOMY_ENABLED=true
GROUP_AUTONOMY_ENABLED=true
GROUP_REPLY_PROBABILITY=0.08
GROUP_REPLY_COOLDOWN_MINUTES=20
MESSAGE_DEBOUNCE_SECONDS=3
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

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
LLM_TIMEOUT_SECONDS = max(5, int(os.getenv("LLM_TIMEOUT_SECONDS", "45")))
LLM_STARTUP_TIMEOUT_SECONDS = max(3, int(os.getenv("LLM_STARTUP_TIMEOUT_SECONDS", "12")))

if LLM_PROVIDER not in {"gemini", "openai_compatible"}:
    raise ValueError("LLM_PROVIDER должен быть gemini или openai_compatible")

ACTIVE_LLM_BASE_URL = GEMINI_BASE_URL if LLM_PROVIDER == "gemini" else LLM_BASE_URL
# Backward compatibility: older installations stored the Gemini key in LLM_API_KEY.
ACTIVE_LLM_API_KEY = (GEMINI_API_KEY or LLM_API_KEY) if LLM_PROVIDER == "gemini" else LLM_API_KEY
ACTIVE_LLM_MODEL = GEMINI_MODEL if LLM_PROVIDER == "gemini" else LLM_MODEL

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
UKRTB_GROUP_LABEL = os.getenv("UKRTB_GROUP_LABEL", "ССА-17-26П").strip()

DB_PATH = DATA_DIR / "ada.db"
SESSION_PATH = DATA_DIR / "ada_userbot"
