"""Реальный end-to-end прогон WS-моста с настоящим Orchestrator + локальной моделью.

Доказывает §5.2: клиент подключается, шлёт команду, получает РЕАЛЬНЫЙ
ответ от backend (не мок). Запуск вручную: python tests/_ws_real_e2e.py
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")
import websockets  # type: ignore

from config import load_config
from core.orchestrator import Orchestrator
from core.ws_server import JarvisWSServer


async def main():
    settings = load_config()
    settings.ensure_directories()
    orch = Orchestrator(settings)
    server = JarvisWSServer(orch, host="127.0.0.1", port=8772)
    server.start()
    await asyncio.sleep(1.0)

    got = []
    try:
        async with websockets.connect("ws://127.0.0.1:8772",
                                      origin="http://localhost:1420") as ws:  # type: ignore
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            print("GREETING:", first)
            await ws.send(json.dumps({"type": "command", "text": "Привет, кратко представься"}))
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                except asyncio.TimeoutError:
                    continue
                got.append(msg)
                if msg.get("type") == "event" and msg["event"]["type"] == "event:jarvis:end":
                    print("REAL RESPONSE:", msg["event"]["payload"].get("content"))
                    break
                if msg.get("type") == "error":
                    print("ERROR FROM SERVER:", msg.get("message"))
                    break
    finally:
        server.shutdown()

    ok = any(m.get("type") == "event" and m["event"]["type"] == "event:jarvis:end" for m in got)
    print("E2E_OK:", ok)


if __name__ == "__main__":
    asyncio.run(main())
