"""Read Windows launch targets and check observable startup state."""

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
from pathlib import Path


SERVICE_START_TIMEOUT = 90


async def _powershell(script, **values):
    process = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script,
        env=os.environ | values, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode:
        raise OSError("PowerShell не смог прочитать состояние запуска")
    return json.loads(stdout.decode("utf-8"))


async def shortcut(path):
    return await _powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ADA_SHORTCUT); "
        "$s | Select-Object TargetPath, WorkingDirectory, Arguments | ConvertTo-Json -Compress",
        ADA_SHORTCUT=str(path),
    )


async def primary_screen_fallback():
    encoded = await _powershell(
        "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; $screen = [Windows.Forms.Screen]::PrimaryScreen; "
        "if (-not $screen) { throw 'No primary monitor' }; $b = $screen.Bounds; "
        "$image = New-Object Drawing.Bitmap($b.Width, $b.Height); "
        "$graphics = [Drawing.Graphics]::FromImage($image); $stream = New-Object IO.MemoryStream; "
        "try { $graphics.CopyFromScreen($b.Location, [Drawing.Point]::Empty, $b.Size); "
        "$image.Save($stream, [Drawing.Imaging.ImageFormat]::Png); "
        "ConvertTo-Json -InputObject ([Convert]::ToBase64String($stream.ToArray())) -Compress } "
        "finally { $graphics.Dispose(); $image.Dispose(); $stream.Dispose() }",
    )
    return base64.b64decode(encoded, validate=True)


async def _matching_processes(target, instance):
    rows = await _powershell(
        "$names = $env:ADA_PROCESS_NAMES.Split(','); "
        "$filter = ($names | ForEach-Object { \"Name = '$_'\" }) -join ' OR '; "
        "$rows = @(Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop | ForEach-Object { "
        "$p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{pid=$_.ProcessId; path=$_.ExecutablePath; cmd=$_.CommandLine; "
        "window=[bool]($p.MainWindowHandle)} }); ConvertTo-Json -InputObject $rows -Compress",
        ADA_PROCESS_NAMES="java.exe,javaw.exe" if instance else target.name.replace("'", "''"),
    )
    if instance:
        name = instance.casefold()
        return {row["pid"] for row in rows if row["window"] and re.search(
            r'[\\/]instances[\\/]' + re.escape(name) + r'(?=[\\/"]|$)',
            (row["cmd"] or "").casefold(),
        )}
    return {row["pid"] for row in rows if row["path"] and Path(row["path"]) == target}


def _server_address(cwd):
    props = cwd / "server.properties"
    if not props.is_file():
        return None
    values = dict(line.split("=", 1) for line in props.read_text(encoding="utf-8").splitlines()
                  if not line.startswith("#") and "=" in line)
    host = values.get("server-ip", "").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    return host, int(values.get("server-port", "25565"))


def _accepts_connections(address):
    try:
        with socket.create_connection(address, timeout=1):
            return True
    except OSError:
        return False


async def open_and_verify(path):
    target, cwd, arguments = path, path.parent, ""
    if path.suffix.lower() == ".lnk":
        link = await shortcut(path)
        if not link["TargetPath"]:
            os.startfile(str(path))
            return f"Ярлык передан Windows: {path.name}. Результат открытия не подтверждён."
        target = Path(link["TargetPath"])
        cwd = Path(link["WorkingDirectory"]) if link["WorkingDirectory"] else target.parent
        arguments = link["Arguments"] or ""
    if not target.exists():
        return f"Цель ярлыка не найдена: {target}."

    address = _server_address(cwd) if target.suffix.lower() in {".bat", ".cmd"} else None
    prism = re.search(r'(?:^|\s)--launch\s+(?:"([^"]+)"|(\S+))', arguments)
    instance = (prism[1] or prism[2]) if prism and target.stem.lower() == "prismlauncher" else None
    check_process = target.suffix.lower() == ".exe"

    if address and await asyncio.to_thread(_accepts_connections, address):
        return f"Порт сервера {address[0]}:{address[1]} уже принимает подключения. Повторный запуск пропущен."
    before = await _matching_processes(target, instance) if check_process else set()
    if instance and before:
        return f"Игра «{instance}» уже запущена: найден процесс с окном (PID {min(before)})."

    # Let Windows apply the shortcut's arguments; use an explicit cwd for direct batch files.
    os.startfile(str(path), cwd=str(cwd))
    if not address and not check_process:
        return f"Запрос открытия передан Windows: {path.name}. Результат открытия не подтверждён."

    deadline = asyncio.get_running_loop().time() + (SERVICE_START_TIMEOUT if address or instance else 8)
    seen = set()
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        if address:
            if await asyncio.to_thread(_accepts_connections, address):
                return f"Проверено: порт сервера {address[0]}:{address[1]} принимает подключения после запуска {path.name}."
            continue
        try:
            current = await _matching_processes(target, instance) - before
        except (OSError, ValueError, TimeoutError):
            return f"Запрос запуска {path.name} передан Windows, но проверить процесс не удалось."
        if current & seen:
            pid = min(current & seen)
            if instance:
                return f"Проверено: игра «{instance}» запущена, у процесса PID {pid} есть окно."
            return f"Проверено: новый процесс {target.name} запущен (PID {pid}). Готовность интерфейса не проверена."
        seen = current
    expected = target.name
    if address:
        expected = f"порт {address[0]}:{address[1]}"
    elif instance:
        expected = f"процесс игры «{instance}» с окном"
    return f"Запрос запуска {path.name} передан Windows, но {expected} не подтвердился за время проверки."
