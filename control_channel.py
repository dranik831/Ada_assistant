"""Private, authenticated loopback channel for an Ada child process."""
import asyncio
import hmac
import json
from contextlib import asynccontextmanager
import logging

logger = logging.getLogger("Ada.Control")


async def request(port, token, command, actor=0, timeout=15):
    async def exchange():
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=128 * 1024)
        try:
            writer.write(json.dumps([token, command, actor]).encode() + b"\n")
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()

    return await asyncio.wait_for(exchange(), timeout)


@asynccontextmanager
async def serve(endpoint, token, edit, stop, ready):
    edit_lock = asyncio.Lock()

    async def handle(reader, writer):
        try:
            supplied, command, actor = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), token.encode()):
                return
            if command == "status":
                reply = "stopping" if stop.is_set() else ("ready" if ready.is_set() else "starting")
            elif command == "stop":
                stop.set()
                reply = "stopping"
            elif not ready.is_set() or stop.is_set():
                reply = "Ада ещё не готова или останавливается."
            elif isinstance(command, str) and len(command) <= 12100 and type(actor) is int and actor > 0:
                async with edit_lock:
                    try:
                        reply = await asyncio.wait_for(edit(command, actor), 60)
                    except Exception as exc:
                        logger.warning("Ошибка команды редактора: %s", type(exc).__name__)
                        reply = "Не удалось выполнить команду редактора."
            else:
                reply = "Недопустимая команда."
            writer.write(json.dumps(reply[:12000], ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (ValueError, TypeError, OSError, TimeoutError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=128 * 1024)
    try:
        endpoint.write_text(str(server.sockets[0].getsockname()[1]), encoding="ascii")
        async with server:
            yield
    finally:
        server.close()
        await server.wait_closed()
        endpoint.unlink(missing_ok=True)
