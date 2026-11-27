"""Самопроверка доступа к управлению ПК: python tests/test_pc_tools.py

execute_system_command/read_file/write_file/list_directory/open_on_pc/open_terminal/
clipboard/show_on_desktop живут в modules/pc_control; send_file остаётся в ядре tools.py.
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import AgentTools  # noqa: E402
from modules.pc_control.tools import PcControlTools  # noqa: E402
from modules.pc_control import windows_launch  # noqa: E402


async def check_launch_verification():
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.bat"
        script.touch()
        (script.parent / "server.properties").write_text("server-port=1488\n", encoding="utf-8")
        with patch.object(windows_launch.os, "startfile", create=True) as start, \
                patch.object(windows_launch, "_accepts_connections", side_effect=[False, True, True]):
            assert "принимает подключения после запуска" in await windows_launch.open_and_verify(script)
            assert "Повторный запуск пропущен" in await windows_launch.open_and_verify(script)
            start.assert_called_once_with(str(script), cwd=str(script.parent))

        with patch.object(windows_launch.os, "startfile", create=True), \
                patch.object(windows_launch, "_accepts_connections", return_value=False), \
                patch.object(windows_launch, "SERVICE_START_TIMEOUT", 0):
            assert "не подтвердился" in await windows_launch.open_and_verify(script)

        launcher = script.with_name("prismlauncher.exe")
        launcher.touch()
        link = script.with_name("Create Aeronautics.lnk")
        metadata = {"TargetPath": str(launcher), "WorkingDirectory": tmp, "Arguments": '--launch "Create Aeronautics"'}
        with patch.object(windows_launch.os, "startfile", create=True), \
                patch.object(windows_launch, "shortcut", new=AsyncMock(return_value=metadata)), \
                patch.object(windows_launch, "_matching_processes", new=AsyncMock(side_effect=[set(), {42}, {42}])) as match:
            result = await windows_launch.open_and_verify(link)
            assert "PID 42" in result and "есть окно" in result
            match.assert_awaited_with(launcher, "Create Aeronautics")


async def main():
    tools = AgentTools(db=None, tg_client=None)
    pc = PcControlTools(pc_allowed=tools._pc_allowed)
    denied = [
        (5, 5, False),     # не Создатель в личке
        (5, -100, True),   # Создатель, но в группе
        (5, 0, True),      # Создатель без чата (автономия/неизвестно)
    ]
    for actor, chat, admin in denied:
        tools.set_request_context(actor, chat, admin)
        for call in (pc.open_on_pc("https://example.com"), pc.open_terminal("echo hi"),
                     pc.clipboard("get"), pc.execute_system_command("echo hi"),
                     pc.read_file(__file__), pc.list_directory("."), pc.write_file("x.txt", "x"),
                     tools.send_file("me", __file__), pc.show_on_desktop("hi"), pc.find_desktop_items("run")):
            result = await call
            assert "только" in result.lower(), (actor, chat, admin, result)

    tools.set_request_context(5, 5, True)
    assert tools._pc_allowed()
    assert "Не нашла" in await pc.open_on_pc("__нет_такого_ярлыка_42__")
    assert "Папка не найдена" in await pc.open_terminal(cwd=str(Path(tempfile.gettempdir()) / "__нет__"))
    assert "get или set" in await pc.clipboard("drop")
    assert "не найден" in await tools.send_file("me", "__нет_такого_файла_42__")
    assert "не запущено" in await pc.show_on_desktop("hi")
    await check_launch_verification()
    print("OK: управление ПК доступно только Создателю в личке")


asyncio.run(main())
