"""Минимальный клиент протокола Source RCON (используется Minecraft).

Протокол: TCP, пакеты длины-префиксом (little-endian int32), поля
request_id/type/payload. Спецификация: https://wiki.vg/RCON
Тип 3 — SERVERDATA_AUTH, тип 2 — SERVERDATA_EXECCOMMAND,
ответ на оба — тип 0 (SERVERDATA_RESPONSE_VALUE), на auth-неудачу request_id = -1.
"""
import asyncio
import struct

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


def _pack(request_id: int, packet_type: int, payload: str) -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    length_bytes = await reader.readexactly(4)
    (length,) = struct.unpack("<i", length_bytes)
    body = await reader.readexactly(length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    payload = body[8:-2].decode("utf-8", errors="replace")
    return request_id, packet_type, payload


class RconClient:
    """Соединение открывается на каждую команду: проще и надёжнее, чем держать
    долгоживущий сокет через рестарты сервера Minecraft."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout

    async def execute(self, command: str) -> str:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        try:
            writer.write(_pack(1, SERVERDATA_AUTH, self.password))
            await writer.drain()
            request_id, _, _ = await asyncio.wait_for(_read_packet(reader), timeout=self.timeout)
            if request_id == -1:
                raise RconAuthError("Неверный RCON-пароль")

            writer.write(_pack(2, SERVERDATA_EXECCOMMAND, command))
            await writer.drain()
            _, _, payload = await asyncio.wait_for(_read_packet(reader), timeout=self.timeout)
            return payload
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
