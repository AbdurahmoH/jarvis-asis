"""P5 §5.2 / §5.7 — транспортный тест WebSocket-моста J.A.R.V.I.S.

Проверяет протокол и маршрутизацию БЕЗ загрузки тяжёлой локальной
модели: используется лёгкий fake-оркестратор, реализующий тот же
публичный контракт, что и настоящий ``Orchestrator``. Это фиксирует
то, что WS-сервер корректно:
  * принимает соединение и приветствует клиента state:idle;
  * перенаправляет "command" в handle_input (единый путь §5.7);
  * транслирует output_callback оркестратора в event:jarvis:end;
  * перенаправляет "confirm" в answer_confirmation;
  * отправляет confirmation_required при needs_confirmation в state.

Без pytest-asyncio: весь тест гонится через asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets  # type: ignore

from config.settings import Settings
from core.ws_server import JarvisWSServer


class FakeOrchestrator:
    """Лёгкая замена Orchestrator для транспортного теста."""

    def __init__(self) -> None:
        self._output_callback = lambda text: None
        self._subscribers = []
        self.commands: list[str] = []
        self.confirms: list[tuple[str, bool]] = []
        self._running = False

    def start(self) -> None:
        self._running = True

    def shutdown(self) -> None:
        self._running = False

    def handle_input(self, text: str):
        self.commands.append(text)
        # Эмулируем синхронный fast-path ответ через output_callback.
        self._output_callback(f"Ответ на: {text}")
        # Тестовое HIGH-risk подтверждение.
        if text.strip().lower().startswith("опасно"):
            return {
                "needs_confirmation": True,
                "confirmation_id": "cid-123",
                "response": "Подтвердите удаление?",
            }
        return {"needs_confirmation": False, "response": f"Ответ на: {text}"}

    def answer_confirmation(self, confirmation_id: str, approved: bool):
        self.confirms.append((confirmation_id, approved))
        self._output_callback(f"Подтверждение {confirmation_id}: {approved}")
        return {
            "response": "Действие подтверждено",
            "assistant_output": {"display_text": "Действие подтверждено", "debug": {}},
            "tool": "test_tool",
            "verified": True,
            "mode": "capability",
        }

    def subscribe_events(self, callback):
        self._subscribers.append(callback)
        return lambda: None

    def list_missions(self, include_terminal=True):
        return []

    def cancel_mission(self, task_id):
        return False


def test_ws_command_roundtrip_and_confirmation():
    """Транспортный тест WS-моста (без pytest-asyncio: гоним через asyncio.run)."""

    async def _run() -> dict:
        orch = FakeOrchestrator()
        # Sprint 5: greeting отключён для чистоты assert'ов.
        orch.settings = Settings()  # noqa: PLW2901
        orch.settings.launcher.greeting_enabled = False
        server = JarvisWSServer(orch, host="127.0.0.1", port=8799)
        server.start()

        received: list[dict] = []

        # Клиент
        # S2: проверка Origin стала fail-closed (пустой список = «никого»), а
        # дефолт конструктора — явный allowlist. Клиент без заголовка Origin
        # теперь отклоняется, как и в продукте, поэтому тест его передаёт.
        async with websockets.connect("ws://127.0.0.1:8799",
                                      origin="http://localhost:1420") as ws:  # type: ignore
            first = json.loads(await ws.recv())
            assert first["type"] == "state" and first["state"] == "idle", first

            # Обычная команда -> ответ
            await ws.send(json.dumps({"type": "command", "text": "привет"}))
            deadline = time.time() + 5
            got_jarvis_end = False
            while time.time() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                except asyncio.TimeoutError:
                    continue
                received.append(msg)
                if msg.get("type") == "event" and msg["event"]["type"] == "event:jarvis:end":
                    got_jarvis_end = True
                    break

            # Опасная команда -> confirmation_required
            await ws.send(json.dumps({"type": "command", "text": "опасно удалить"}))
            deadline = time.time() + 5
            cid = None
            while time.time() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                except asyncio.TimeoutError:
                    continue
                received.append(msg)
                if msg.get("type") == "confirmation_required":
                    cid = msg["confirmation_id"]
                    await ws.send(json.dumps({
                        "type": "confirm",
                        "confirmation_id": cid,
                        "approve": True,
                    }))
                    deadline2 = time.time() + 5
                    while time.time() < deadline2:
                        try:
                            followup = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                        except asyncio.TimeoutError:
                            continue
                        received.append(followup)
                        if followup.get("type") == "assistant_output":
                            break
                    break

        server.shutdown()
        return {
            "commands": orch.commands,
            "confirms": orch.confirms,
            "received": received,
            "got_jarvis_end": got_jarvis_end,
            "cid": cid,
        }

    result = asyncio.run(_run())

    # Доказательства:
    assert "привет" in result["commands"], "команда дошла до handle_input"
    assert result["got_jarvis_end"], "реальный ответ от backend транслирован в event"
    assert any(m.get("type") == "confirmation_required" for m in result["received"]), \
        "HIGH-risk подтверждение дошло до клиента"
    assert ("cid-123", True) in result["confirms"], \
        "confirm перенаправлен в answer_confirmation"
    assert any(
        m.get("type") == "assistant_output"
        and m.get("output", {}).get("display_text") == "Действие подтверждено"
        for m in result["received"]
    ), "результат подтверждённого действия возвращён в тот же WebSocket"
