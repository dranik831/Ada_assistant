"""Telegram Mini App served on loopback, behind a user-managed HTTPS proxy."""
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from urllib.parse import parse_qsl

from aiohttp import web
from dotenv import dotenv_values
from dotenv.parser import parse_stream


def parse_ids(raw):
    ids = {int(item) for item in (raw or "").split(",") if item.strip()}
    if not ids or any(not 0 < uid < 2**52 for uid in ids):
        raise ValueError("Укажи положительные Telegram ID через запятую.")
    return ids


def telegram_actor(init_data, token, now=None):
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
    data = dict(pairs)
    if len(pairs) != len(data):
        raise ValueError("Duplicate fields")
    supplied = data.pop("hash", "")
    canonical = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    expected = hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", supplied) or not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid signature")
    if not -30 <= (time.time() if now is None else now) - int(data["auth_date"]) <= 1800:
        raise ValueError("Expired session")
    user = json.loads(data["user"])
    actor = user["id"]
    if type(actor) is not int or actor <= 0 or user.get("is_bot"):
        raise ValueError("Invalid actor")
    return actor


_SECRET = re.compile(r"KEY|TOKEN|PASSWORD|SECRET|HASH|PHONE|CREDENTIAL|COOKIE|SESSION", re.I)
_SAFE_PREFIX = ("TG_", "ADMIN_", "LLM_", "ANTHROPIC_", "GEMINI_", "MODULE_", "CONTROL_", "ADA_",
                "MINECRAFT_", "EMAIL_", "VISION_", "WHISPER_", "UKRTB_", "AUTONOMY_", "GROUP_", "CONTEXT_")
_INTERNAL = {"ADA_CONTROL_ENDPOINT", "ADA_CONTROL_SECRET"}


class EnvEditor:
    def __init__(self, path, template, owner):
        self.path, self.owner = path, owner
        self.known = set(dotenv_values(template, interpolate=False))

    def values(self):
        return dotenv_values(self.path, interpolate=False)

    def allowed(self):
        try:
            return parse_ids(self.values().get("CONTROL_BOT_ALLOWED_IDS"))
        except (OSError, ValueError):
            return set()

    def snapshot(self):
        values = self.values()
        return [dict(key=key, secret=hidden, value=None if hidden else values.get(key),
                     configured=bool(values.get(key)), editable=self.editable(key))
                for key in sorted(self.known | values.keys())
                for hidden in [bool(_SECRET.search(key)) or not (key in self.known or key.startswith(_SAFE_PREFIX))]]

    def editable(self, key):
        return key not in _INTERNAL and (key in self.known or key.startswith(_SAFE_PREFIX))

    def save(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Нет изменений.")
        current = self.values()
        available = self.known | current.keys()
        for key, value in changes.items():
            if key not in available or not self.editable(key):
                raise ValueError("Эту переменную можно изменить только локально.")
            if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "\x00\r\n"):
                raise ValueError("Значение должно быть одной строкой до 4096 символов.")
        if {"CONTROL_BOT_ALLOWED_IDS", "ADMIN_USER_ID"} & changes.keys():
            updated = current | changes
            allowed = parse_ids(updated.get("CONTROL_BOT_ALLOWED_IDS"))
            if self.owner not in allowed or int(updated.get("ADMIN_USER_ID") or 0) not in allowed:
                raise ValueError("Текущий и новый владелец должны остаться в белом списке.")
        if "CONTROL_WEB_PORT" in changes and not (changes["CONTROL_WEB_PORT"].isdigit() and 1 <= int(changes["CONTROL_WEB_PORT"]) <= 65535):
            raise ValueError("Порт должен быть от 1 до 65535.")
        pending = dict(changes)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".env-edit-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output, self.path.open(encoding="utf-8", newline="") as source:
                for binding in parse_stream(source):
                    if binding.key not in changes:
                        output.write(binding.original.string)
                    elif binding.key in pending:
                        output.write(self.line(binding.key, pending.pop(binding.key)))
                if pending:
                    output.write("\n")
                    for key, value in pending.items():
                        output.write(self.line(key, value))
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @staticmethod
    def line(key, value):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"{key}='{escaped}'\n"


def make_app(*, token, env, ada, logs, assets, levels):
    @web.middleware
    async def access(request, handler):
        if request.path.startswith("/api/"):
            try:
                actor = telegram_actor(request.headers.get("X-Telegram-Init-Data", ""), token)
            except (ValueError, KeyError, TypeError, OverflowError):
                return web.json_response({"error": "Открой панель из Telegram заново."}, status=401,
                                         headers={"Cache-Control": "no-store"})
            if actor not in env.allowed():
                return web.json_response({"error": "Нет доступа."}, status=403,
                                         headers={"Cache-Control": "no-store"})
            request["actor"] = actor
        try:
            response = await handler(request)
        except (ValueError, TypeError) as exc:
            response = web.json_response({"error": str(exc) if isinstance(exc, EnvInputError) else "Проверь параметры."}, status=400)
        except (OSError, TimeoutError):
            response = web.json_response({"error": "Ада не ответила или ещё запускается."}, status=503)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"})
        return response

    async def status(request):
        return web.json_response({"status": await ada.status(), "owner": request["actor"] == env.owner})

    async def action(request):
        data = await request.json()
        name = data.get("action") if isinstance(data, dict) else None
        if name not in ("start", "stop", "restart", "maintenance"):
            raise ValueError
        return web.json_response({"result": await ada.manage(name, request["actor"])})

    async def edit(request):
        data = await request.json()
        text = data.get("command") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 12100:
            raise ValueError
        return web.json_response({"result": await ada.call(text.strip(), request["actor"], timeout=65)})

    async def settings(request):
        if request["actor"] != env.owner:
            return web.json_response({"error": "Настройки доступны только владельцу."}, status=403)
        if request.method == "POST":
            data = await request.json()
            try:
                env.save(data.get("changes") if isinstance(data, dict) else None)
            except ValueError as exc:
                raise EnvInputError(str(exc)) from None
        return web.json_response({"fields": env.snapshot()})

    async def log_view(request):
        selected = set(request.query.get("levels", "").split(",")) & levels
        return web.json_response({"rows": [row for row in logs if row[0] in selected]})

    app = web.Application(middlewares=[access], client_max_size=128 * 1024)
    for url, file in [("/", "index.html"), ("/app.js", "app.js"), ("/style.css", "style.css")]:
        async def asset(request, name=file):
            return web.FileResponse(assets / name)
        app.router.add_get(url, asset)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/action", action)
    app.router.add_post("/api/edit", edit)
    app.router.add_get("/api/env", settings)
    app.router.add_post("/api/env", settings)
    app.router.add_get("/api/logs", log_view)
    return app


class EnvInputError(ValueError):
    pass
