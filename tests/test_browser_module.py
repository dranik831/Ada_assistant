"""Browser checks. Pass --chromium to exercise the installed browser as well."""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import module_system
from llm import NormalizedMessage, UniversalAgent
from modules.browser_control import BrowserSession, _allowed_url, setup
from tools import AgentTools


def browser_tools(tools):
    registry = module_system.ModuleRegistry(services={"tools": tools})
    registry.modules["browser_control"] = module_system.ModuleInfo(name="browser_control", enabled=True)
    setup(module_system.ModuleContext(registry, "browser_control"))
    return registry.tool_functions()


async def check_permissions():
    tools = AgentTools(None, None)
    actions = browser_tools(tools)
    assert {
        "browser_open", "browser_snapshot", "browser_navigate", "browser_screenshot",
        "browser_click", "browser_type", "browser_key", "browser_scroll", "browser_move", "browser_close",
    } == actions.keys()
    for context in [(5, 5, True, False), (5, -5, True, True), (5, 5, False, True), (5, 6, True, True)]:
        actor, chat, admin, browser = context
        tools.set_request_context(actor, chat, admin, allow_browser=browser)
        assert "только Создателю" in await actions["browser_open"]("https://example.com")
    assert _allowed_url("https://example.com")
    for url in ("file:///x", "https://@example.com", "https://user:pass@example.com", "http://["):
        assert not _allowed_url(url)


def test_browser_permissions():
    asyncio.run(check_permissions())


async def check_tool_isolation():
    tools = AgentTools(None, None)
    tools.set_request_context(5, 5, True, allow_browser=True)
    agent = UniversalAgent.__new__(UniversalAgent)
    agent.tools = tools
    outside = AsyncMock()
    screenshot = AsyncMock(return_value="На странице видно заголовок.")
    agent.tool_functions = {"outside_tool": outside, "browser_screenshot": screenshot}
    agent._completion = AsyncMock(side_effect=[
        NormalizedMessage("", [{"id": "discover", "name": "list_module_tools",
                               "arguments": {"module_name": "browser_control"}}]),
        NormalizedMessage("", [
            {"id": "outside", "name": "outside_tool", "arguments": {}},
            {"id": "image", "name": "browser_screenshot", "arguments": {}},
        ]),
        NormalizedMessage("done", []),
    ])
    registry = module_system.ModuleRegistry(services={"tools": tools})
    registry.modules["browser_control"] = module_system.ModuleInfo(name="browser_control", enabled=True)
    setup(module_system.ModuleContext(registry, "browser_control"))
    with patch.object(module_system, "registry", return_value=registry):
        await agent.run_turn("test", "Посмотри, как выглядит текущая страница")
    outside.assert_not_awaited()
    screenshot.assert_awaited_once_with()
    first_specs = agent._completion.await_args_list[0].kwargs["tools"]
    assert any(spec["function"]["name"] == "fetch_web_page" for spec in first_specs)
    specs = agent._completion.await_args.kwargs["tools"]
    assert specs and all(spec["function"]["name"].startswith("browser_") for spec in specs)


def test_browser_tool_isolation():
    asyncio.run(check_tool_isolation())


async def check_chromium(root):
    tools = AgentTools(None, None)
    tools.set_request_context(5, 5, True, allow_browser=True)
    session = BrowserSession(root)
    with patch("modules.browser_control.BrowserSession", return_value=session):
        actions = browser_tools(tools)
    try:
        page = await session.start()
        await page.route("https://example.com/", lambda route: route.fulfill(
            content_type="text/html", body='<title>Fixture</title><input style="position:absolute;left:10px;top:10px">',
        ))
        assert "Fixture" in await actions["browser_open"]("https://example.com/")
        assert "Курсор перемещён" in await actions["browser_move"](20, 20)
        assert "Клик выполнен" in await actions["browser_click"](20, 20)
        assert "Введено 3" in await actions["browser_type"]("Ада")
        assert await page.locator("input").input_value() == "Ада"
        assert "Backspace" in await actions["browser_key"]("Backspace")
        assert await page.locator("input").input_value() == "Ад"
        with patch("modules.vision.engine.describe", new=AsyncMock(return_value="Image received")) as describe:
            assert "Image received" in await actions["browser_screenshot"]()
            assert describe.await_args.args[0].startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        await actions["browser_close"]()
    assert session.page is None


if __name__ == "__main__":
    test_browser_permissions()
    test_browser_tool_isolation()
    if "--chromium" in sys.argv:
        with TemporaryDirectory() as folder:
            asyncio.run(check_chromium(Path(folder)))
    print("browser module checks passed")
