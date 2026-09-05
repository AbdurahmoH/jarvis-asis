"""P5 §5.3 — настройки облачного профиля через WebSocket.

Проверяет реальный WS-протокол, но использует лёгкий fake-оркестратор.
Рабочий settings.json создаётся в pytest tmp_path: реальные ключи и
config/settings.json проекта не затрагиваются.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import websockets  # type: ignore

from config.settings import Settings
from core.ws_server import JarvisWSServer


class SettingsOrchestrator:
    """Минимальный публичный контракт Orchestrator для настроек WS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._output_callback = lambda text: None

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def subscribe_events(self, callback):
        return lambda: None

    def list_missions(self, include_terminal=True):
        return []

    def cancel_mission(self, task_id):
        return False


async def _receive_type(ws, expected: str) -> dict:
    """Читает до сообщения нужного типа, не маскируя неожиданные сообщения."""
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
        if message.get("type") == expected:
            return message
    raise AssertionError(f"Не получено WS-сообщение {expected!r}")


async def _connect(port: int):
    """Коротко ждёт поток сервера — без зависимости от планировщика Windows."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            # S2: Origin обязателен — проверка стала fail-closed.
            return await websockets.connect(f"ws://127.0.0.1:{port}",
                                            origin="http://localhost:1420")  # type: ignore
        except OSError:
            await asyncio.sleep(0.03)
    raise AssertionError("WS-сервер не начал слушать порт")


def test_ws_cloud_settings_are_masked_persisted_and_preserved(tmp_path: Path):
    """WS не выдаёт ключ и атомарно сохраняет/обновляет cloud-профиль."""
    config_path = tmp_path / "settings.json"
    settings = Settings()
    settings.source_path = config_path
    settings.api_keys.deepinfra = "old-secret-key"
    settings.launcher.greeting_enabled = False
    settings.save_config()

    async def _run() -> dict:
        server = JarvisWSServer(SettingsOrchestrator(settings), host="127.0.0.1", port=8800)
        server.start()
        try:
            async with await _connect(8800) as ws:
                greeting = json.loads(await ws.recv())
                assert greeting == {"type": "state", "state": "idle"}

                await ws.send(json.dumps({"type": "settings:get"}))
                before = await _receive_type(ws, "settings")

                await ws.send(json.dumps({
                    "type": "settings:update",
                    "settings": {
                        "provider": "openrouter",
                        "base_url": "https://openrouter.ai/api/v1/",
                        "model": "openai/gpt-4.1-mini",
                        "api_key": "new-secret-key",
                    },
                }))
                saved = await _receive_type(ws, "settings:saved")

                # Пустое поле password — намеренно «не менять ключ».
                await ws.send(json.dumps({
                    "type": "settings:update",
                    "settings": {"api_key": ""},
                }))
                preserved = await _receive_type(ws, "settings:saved")

                await ws.send(json.dumps({"type": "settings:get"}))
                after = await _receive_type(ws, "settings")
        finally:
            server.shutdown()

        return {
            "before": before,
            "saved": saved,
            "preserved": preserved,
            "after": after,
        }

    result = asyncio.run(_run())
    disk = json.loads(config_path.read_text(encoding="utf-8"))

    for payload in (result["before"], result["saved"], result["preserved"], result["after"]):
        serialized = json.dumps(payload)
        assert "old-secret-key" not in serialized
        assert "new-secret-key" not in serialized

    assert result["before"]["settings"]["has_api_key"] is True
    assert result["before"]["settings"]["api_key_masked"].endswith("-key")
    assert result["saved"]["ok"] is True
    assert result["preserved"]["ok"] is True
    assert result["after"]["settings"] == {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4.1-mini",
        "has_api_key": True,
        "api_key_masked": "••••-key",
    }
    assert disk["tier_providers"]["analyst"] == "openrouter"
    assert disk["api_endpoints"]["openrouter"] == "https://openrouter.ai/api/v1"
    assert disk["model_tiers"]["analyst"] == "openai/gpt-4.1-mini"
    assert disk["api_keys"]["openrouter"] == "new-secret-key"
    assert not config_path.with_suffix(".json.tmp").exists()


def test_ws_cloud_settings_reject_invalid_provider_without_writing(tmp_path: Path):
    """Недопустимый provider возвращает ошибку и не портит settings.json."""
    config_path = tmp_path / "settings.json"
    settings = Settings()
    settings.source_path = config_path
    settings.launcher.greeting_enabled = False
    settings.save_config()
    original = config_path.read_text(encoding="utf-8")

    async def _run() -> dict:
        server = JarvisWSServer(SettingsOrchestrator(settings), host="127.0.0.1", port=8801)
        server.start()
        try:
            async with await _connect(8801) as ws:
                await ws.recv()
                await ws.send(json.dumps({
                    "type": "settings:update",
                    "settings": {"provider": "../../not-a-provider"},
                }))
                return await _receive_type(ws, "error")
        finally:
            server.shutdown()

    error = asyncio.run(_run())
    assert "provider" in error["message"].lower()
    assert config_path.read_text(encoding="utf-8") == original
