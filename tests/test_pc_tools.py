"""Самопроверка доступа к управлению ПК: python tests/test_pc_tools.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import AgentTools  # noqa: E402


async def main():
    tools = AgentTools(db=None, tg_client=None)
    denied = [
        (5, 5, False),     # не Создатель в личке
        (5, -100, True),   # Создатель, но в группе
        (5, 0, True),      # Создатель без чата (автономия/неизвестно)
    ]
    for actor, chat, admin in denied:
        tools.set_request_context(actor, chat, admin)
        for call in (tools.open_on_pc("https://example.com"), tools.open_terminal("echo hi"),
                     tools.clipboard("get"), tools.execute_system_command("echo hi"),
                     tools.read_file(__file__), tools.list_directory("."), tools.write_file("x.txt", "x"),
                     tools.send_file("me", __file__), tools.show_on_desktop("hi")):
            result = await call
            assert "только" in result.lower(), (actor, chat, admin, result)

    tools.set_request_context(5, 5, True)
    assert tools._pc_allowed()
    assert "Не нашла" in await tools.open_on_pc("__нет_такого_ярлыка_42__")
    assert "Папка не найдена" in await tools.open_terminal(cwd=str(Path(tempfile.gettempdir()) / "__нет__"))
    assert "get или set" in await tools.clipboard("drop")
    assert "не найден" in await tools.send_file("me", "__нет_такого_файла_42__")
    assert "не запущено" in await tools.show_on_desktop("hi")
    print("OK: управление ПК доступно только Создателю в личке")


asyncio.run(main())
