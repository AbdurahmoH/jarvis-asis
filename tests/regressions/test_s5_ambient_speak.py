"""Regression S5 — ambient-инициатива звучит, а не молча умирает.

Аудит 2026-09-05: ветка ``ambient_initiated`` в ``core/ws_server.py``
вызывала ``self._speak(text)`` со строкой, а ``_speak`` принимает только
``AssistantOutput`` и до правки глотал поднявшийся ``TypeError`` на уровне
``log.debug``. Фича была мертва и молчала об этом: событие ``system_initiated``
клиент получал, очередь TTS — никогда.

Закрепляется: (1) ambient-событие с текстом кладёт ``AssistantOutput`` в
очередь TTS; (2) событие по-прежнему уходит клиенту (прежнее поведение);
(3) пустой текст не издаёт ни события, ни речи; (4) ``_speak`` отвергает
неверный тип громко (``log.error``), ничего не кладя в очередь — положительный
контроль показывает, что корректный ``AssistantOutput`` проходит; (5)
структурно: ни один вызов ``_speak`` в ``ws_server.py`` не передаёт строковый
литерал — путь «строка мимо границы» не вернётся молча.
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings import Settings
from core.voice.output import AssistantOutput
from core.ws_server import JarvisWSServer

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WS_SRC = _REPO_ROOT / "core" / "ws_server.py"


class _TtsQueue:
    """Двойник очереди TTS: запоминает, что в неё положили."""

    def __init__(self) -> None:
        self.outputs: list[Any] = []
        self.interrupted = 0

    def add_output(self, output: Any) -> None:
        self.outputs.append(output)

    def interrupt(self) -> None:
        self.interrupted += 1


class _FakeWS:
    """Двойник соединения: складывает отправленные кадры."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _server(queue: _TtsQueue) -> JarvisWSServer:
    orch = SimpleNamespace(
        _settings=Settings(),
        _tts_queue=queue,
        _output_callback=lambda text: None,
    )
    return JarvisWSServer(orch, auth_token=None)


def _send(server: JarvisWSServer, ws: _FakeWS, payload: dict[str, Any]) -> None:
    asyncio.run(server._on_message(ws, json.dumps(payload)))


def _frames(ws: _FakeWS) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in ws.sent]


def test_ambient_initiated_reaches_the_tts_queue() -> None:
    queue = _TtsQueue()
    server = _server(queue)
    ws = _FakeWS()

    _send(server, ws, {"type": "ambient_initiated", "text": "Пора сделать перерыв"})

    assert len(queue.outputs) == 1
    spoken = queue.outputs[0]
    assert isinstance(spoken, AssistantOutput)
    assert "Пора сделать перерыв" in spoken.text


def test_ambient_event_still_reaches_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Событие system_initiated уходит в рассылку с тем же текстом.

    ``_emit`` в юнит-окружении — no-op (требует живого цикла сервера), поэтому
    перехватываем его двойником: проверяем, что ветка вызвала рассылку с
    корректным payload, не воспроизводя жизненный цикл сокета.
    """
    queue = _TtsQueue()
    server = _server(queue)
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "_emit", emitted.append)
    ws = _FakeWS()

    _send(server, ws, {"type": "ambient_initiated", "text": "Напоминание сработало"})

    events = [f for f in emitted
              if f.get("event", {}).get("type") == "event:system_initiated"]
    assert len(events) == 1
    assert events[0]["event"]["payload"]["text"] == "Напоминание сработало"
    assert len(queue.outputs) == 1, "озвучка и событие происходят вместе"


def test_ambient_empty_text_stays_silent() -> None:
    queue = _TtsQueue()
    server = _server(queue)
    ws = _FakeWS()

    _send(server, ws, {"type": "ambient_initiated", "text": "   "})

    assert queue.outputs == []
    assert ws.sent == []


def test_speak_rejects_a_raw_string_loudly(caplog: pytest.LogCaptureFixture) -> None:
    queue = _TtsQueue()
    server = _server(queue)

    with caplog.at_level(logging.DEBUG):
        server._speak("это не AssistantOutput")

    assert queue.outputs == []
    errors = [r for r in caplog.records
              if r.levelno == logging.ERROR and "TTS-озвучка" in r.message]
    assert errors, "TypeError на границе речи должен логироваться как error"
    assert not any(r.levelno == logging.DEBUG and "TTS-озвучка отклонена" in r.message
                   for r in caplog.records), "отказ по типу не должен глотаться на debug"


def test_speak_accepts_a_typed_output() -> None:
    queue = _TtsQueue()
    server = _server(queue)

    server._speak(AssistantOutput.natural("Приветствую"))

    assert len(queue.outputs) == 1


def test_no_speak_call_passes_a_string_literal() -> None:
    """AST-инвариант: путь «строка мимо границы речи» не вернётся молча."""

    tree = ast.parse(_WS_SRC.read_text(encoding="utf-8"))
    calls: list[ast.Call] = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_speak"
    ]
    assert calls, "в ws_server.py должен остаться хотя бы один вызов _speak"
    for node in calls:
        first = node.args[0] if node.args else None
        assert not isinstance(first, ast.Constant) or not isinstance(first.value, str), (
            f"_speak вызывается со строковым литералом (строка {node.lineno}) — "
            "граница речи принимает только AssistantOutput"
        )
