"""Управляемый Chromium-браузер для Создателя.

Модуль намеренно работает только в личном чате Создателя. Он использует
Playwright для одной постоянной вкладки: переходов, скриншотов, DOM-снимка,
кликов, ввода текста, клавиш и прокрутки.
"""

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlsplit

from module_system import HOOK_SHUTDOWN, ModuleContext

DESCRIPTION = "управление Chromium: страницы, скриншоты, мышь, клавиатура и формы"
VERSION = "1.0"
DEFAULT_ENABLED = False

logger = logging.getLogger("Ada.Module.browser_control")


class BrowserSession:
    def __init__(self, root: Path):
        self.root = root
        self.profile = root / "browser-profile"
        self.shots = root / "browser-screenshots"
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        from playwright.async_api import async_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.profile), headless=False, viewport={"width": 1280, "height": 800},
            args=["--disable-notifications"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self.page

    async def close(self):
        async with self.lock:
            if self.context:
                await self.context.close()
            if self.playwright:
                await self.playwright.stop()
            self.page = self.context = self.playwright = None


def _allowed_url(url: str) -> bool:
    parsed = urlsplit((url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def _number(value, minimum, maximum):
    value = float(value)
    if not minimum <= value <= maximum:
        raise ValueError(f"Значение должно быть от {minimum} до {maximum}.")
    return value


def setup(ctx: ModuleContext) -> None:
    tools = ctx.tools
    if tools is None:
        return
    session = BrowserSession(ctx.config.DATA_DIR)

    def allowed():
        return tools._pc_allowed()

    async def page_info(page):
        return f"URL: {page.url}\nЗаголовок: {(await page.title())[:300]}"

    async def browser_open(url: str) -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        if not _allowed_url(url):
            return "Разрешены только обычные http:// и https:// URL без логина и пароля."
        try:
            async with session.lock:
                page = await session.start()
                await page.goto(url.strip(), wait_until="domcontentloaded", timeout=30000)
                return await page_info(page)
        except Exception as exc:
            return f"Не удалось открыть страницу: {type(exc).__name__}: {exc}"[:1200]

    async def browser_snapshot() -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        try:
            async with session.lock:
                page = await session.start()
                text = await page.locator("body").inner_text(timeout=10000)
                return (await page_info(page) + "\n\nТекст страницы:\n" + text[:10000])
        except Exception as exc:
            return f"Не удалось получить страницу: {type(exc).__name__}: {exc}"[:1200]

    async def browser_navigate(action: str) -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        if action not in {"back", "forward", "reload"}:
            return "action должен быть back, forward или reload."
        try:
            async with session.lock:
                page = await session.start()
                if action == "back":
                    await page.go_back(wait_until="domcontentloaded", timeout=30000)
                elif action == "forward":
                    await page.go_forward(wait_until="domcontentloaded", timeout=30000)
                else:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                return await page_info(page)
        except Exception as exc:
            return f"Навигация не удалась: {type(exc).__name__}: {exc}"[:1200]

    async def browser_screenshot(question: str = "") -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        try:
            async with session.lock:
                page = await session.start()
                path = session.shots / "latest.png"
                await page.screenshot(path=str(path), full_page=False)
                info = await page_info(page)
            try:
                from modules.vision.engine import describe
                prompt = (question or "Что находится на странице и какие элементы управления видны?") + " Отвечай по-русски."
                description = await describe(path.read_bytes(), prompt)
                return f"{info}\nОписание скриншота:\n{description}\nФайл скриншота: {path}"
            except Exception as exc:
                return f"{info}\nСкриншот сохранён: {path}\nVision недоступен: {type(exc).__name__}"
        except Exception as exc:
            return f"Не удалось сделать скриншот: {type(exc).__name__}: {exc}"[:1200]

    async def browser_click(x: float, y: float, button: str = "left", clicks: int = 1) -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        if button not in {"left", "middle", "right"} or int(clicks) not in {1, 2, 3}:
            return "button: left/middle/right; clicks: 1, 2 или 3."
        try:
            async with session.lock:
                page = await session.start()
                await page.mouse.click(_number(x, 0, 1280), _number(y, 0, 800), button=button, click_count=int(clicks))
                return f"Клик выполнен в ({x}, {y}).\n{await page_info(page)}"
        except Exception as exc:
            return f"Не удалось кликнуть: {type(exc).__name__}: {exc}"[:1200]

    async def browser_type(text: str, x: float = -1, y: float = -1, submit: bool = False) -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        if len(text or "") > 4000:
            return "Текст слишком длинный (максимум 4000 символов)."
        try:
            async with session.lock:
                page = await session.start()
                if x >= 0 and y >= 0:
                    await page.mouse.click(_number(x, 0, 1280), _number(y, 0, 800))
                await page.keyboard.insert_text(text or "")
                if submit:
                    await page.keyboard.press("Enter")
                return f"Введено {len(text or '')} символов." + (" Нажат Enter." if submit else "")
        except Exception as exc:
            return f"Не удалось ввести текст: {type(exc).__name__}: {exc}"[:1200]

    async def browser_key(key: str) -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        if not key or len(key) > 40 or any(char in key for char in "\r\n"):
            return "Некорректная клавиша."
        try:
            async with session.lock:
                page = await session.start()
                await page.keyboard.press(key)
                return f"Нажата клавиша: {key}."
        except Exception as exc:
            return f"Не удалось нажать клавишу: {type(exc).__name__}: {exc}"[:1200]

    async def browser_scroll(delta_y: float = 600) -> str:
        if not allowed():
            return "Браузер доступен только Создателю в личном чате."
        try:
            async with session.lock:
                page = await session.start()
                await page.mouse.wheel(0, _number(delta_y, -10000, 10000))
                return "Страница прокручена."
        except Exception as exc:
            return f"Не удалось прокрутить: {type(exc).__name__}: {exc}"[:1200]

    props_xy = {"x": {"type": "number"}, "y": {"type": "number"}}
    ctx.register_tool("browser_open", browser_open, "Открыть http/https страницу в управляемом Chromium.", {"url": {"type": "string"}}, ["url"])
    ctx.register_tool("browser_snapshot", browser_snapshot, "Получить URL, заголовок и текст текущей страницы.", {})
    ctx.register_tool("browser_navigate", browser_navigate, "Перейти назад/вперёд или обновить текущую страницу.", {"action": {"type": "string", "enum": ["back", "forward", "reload"]}}, ["action"])
    ctx.register_tool("browser_screenshot", browser_screenshot, "Сделать скриншот текущей страницы и описать его vision-моделью.", {"question": {"type": "string"}})
    ctx.register_tool("browser_click", browser_click, "Кликнуть мышью по координатам скриншота (1280x800).", {**props_xy, "button": {"type": "string", "enum": ["left", "middle", "right"]}, "clicks": {"type": "integer"}}, ["x", "y"])
    ctx.register_tool("browser_type", browser_type, "Вставить текст в сфокусированное поле; x/y необязательны для предварительного клика.", {"text": {"type": "string"}, **props_xy, "submit": {"type": "boolean"}}, ["text"])
    ctx.register_tool("browser_key", browser_key, "Нажать клавишу или комбинацию Playwright, например Enter или Control+A.", {"key": {"type": "string"}}, ["key"])
    ctx.register_tool("browser_scroll", browser_scroll, "Прокрутить текущую страницу.", {"delta_y": {"type": "number"}})
    ctx.register_capability("browser_control: управляемый Chromium со скриншотами, мышью, клавиатурой и заполнением полей (только Создатель).")
    ctx.register_hook(HOOK_SHUTDOWN, session.close)
