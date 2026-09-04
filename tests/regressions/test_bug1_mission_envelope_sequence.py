"""Regression: БАГ 1 + БАГ 2 — последовательность WS-конвертов миссии.

Фейковый клиент собирает все конверты через перехват _emit и проверяет:
1. Полная стримленная миссия: ack → start → tokens → end, без дублей.
2. ACKNOWLEDGED не порождает event:jarvis (ACK не дублируется пузырём).
3. Нестримленная миссия: _cb создаёт ровно одну пару start+end,
   EVENT_TASK_COMPLETED не создаёт вторую карточку.
4. Стримленная миссия, финал из другого потока: пузырь закрывается ровно
   один раз тем же correlation id (mission_id).
5. EVENT_TASK_FAILED закрывает пузырь ровно один раз.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest


def _make_server():
    """Реальный JarvisWSServer поверх mock-оркестратора: _cb существует как в бою."""
    from core.ws_server import JarvisWSServer
    from config.settings import Settings

    settings = Settings()
    orch = MagicMock()
    orch._settings = settings
    orch._output_callback = lambda x: None
    orch.proactor = None

    server = JarvisWSServer(orch)

    emitted: list[dict] = []
    server._emit = emitted.append  # type: ignore[method-assign]
    # _cb — замыкание из __init__, установленное как orch._output_callback.
    return server, emitted, orch


def _jarvis_events(emitted):
    out = []
    for envelope in emitted:
        if envelope.get("type") == "event":
            etype = str((envelope.get("event") or {}).get("type") or "")
            if etype.startswith("event:jarvis"):
                out.append((etype, (envelope.get("event") or {}).get("payload") or {}))
    return out


def _task_event(etype: str, task_id: str, payload: dict | None = None):
    from core.task_runtime import TaskEvent

    return TaskEvent(task_id=task_id, event_type=etype, payload=dict(payload or {}))


def test_streamed_mission_sequence_ack_start_tokens_end_no_duplicates():
    server, emitted, _orch = _make_server()
    from core.task_runtime import (
        EVENT_ACKNOWLEDGED, EVENT_STREAM_CHUNK, EVENT_STREAM_END, EVENT_TASK_COMPLETED,
    )

    server._on_task_event(_task_event(EVENT_ACKNOWLEDGED, "T1"))
    server._on_task_event(_task_event(EVENT_STREAM_CHUNK, "T1", {"text": "при"}))
    server._on_task_event(_task_event(EVENT_STREAM_CHUNK, "T1", {"text": "вет"}))
    server._on_task_event(_task_event(EVENT_STREAM_END, "T1", {"text": "привет"}))
    server._on_task_event(_task_event(EVENT_TASK_COMPLETED, "T1", {"result": "привет", "status": "completed"}))

    events = _jarvis_events(emitted)
    starts = [e for e in events if e[0] == "event:jarvis:start"]
    tokens = [e for e in events if e[0] == "event:jarvis:token"]
    ends = [e for e in events if e[0] == "event:jarvis:end"]
    assert len(starts) == 1, f"start отправлен {len(starts)} раз: {starts}"
    assert len(tokens) == 2
    assert len(ends) == 1, f"end отправлен {len(ends)} раз: {ends}"
    assert starts[0][1]["id"] == "T1" and ends[0][1]["id"] == "T1"
    # ACK не порождает jarvis-конвертов (БАГ 1: двойной ACK).
    states = [e["state"] for e in emitted if e.get("type") == "state"]
    assert states[0] == "thinking"
    assert "idle" in states


def test_ack_emits_no_jarvis_envelope():
    server, emitted, _orch = _make_server()
    from core.task_runtime import EVENT_ACKNOWLEDGED

    server._on_task_event(_task_event(EVENT_ACKNOWLEDGED, "T9"))
    assert _jarvis_events(emitted) == [], "ACK не должен порождать event:jarvis"


def test_non_streamed_mission_single_bubble():
    server, emitted, orch = _make_server()
    orch.consume_streamed_mission = MagicMock(return_value=None)
    mission = MagicMock()
    mission.metadata = {"_output_sent": True}
    orch.get_mission = MagicMock(return_value=mission)

    orch._output_callback("итог работы")
    from core.task_runtime import EVENT_TASK_COMPLETED

    server._on_task_event(_task_event(
        EVENT_TASK_COMPLETED, "T2", {"result": "итог работы", "status": "completed"},
    ))

    events = _jarvis_events(emitted)
    starts = [e for e in events if e[0] == "event:jarvis:start"]
    ends = [e for e in events if e[0] == "event:jarvis:end"]
    assert len(starts) == 1, f"дубль start: {starts}"
    assert len(ends) == 1, f"дубль end: {ends}"


def test_streamed_final_from_another_thread_closes_bubble_once():
    """Финальный текст приходит из потока без tls.cid — resolve по mission_id."""
    server, emitted, orch = _make_server()
    server._streaming_started.add("T3")
    orch.consume_streamed_mission = MagicMock(return_value="T3")

    done = threading.Event()

    def _run():
        try:
            orch._output_callback("финал миссии")
        finally:
            done.set()

    threading.Thread(target=_run).start()
    done.wait(timeout=2.0)
    from core.task_runtime import EVENT_TASK_COMPLETED

    server._on_task_event(_task_event(
        EVENT_TASK_COMPLETED, "T3", {"result": "финал миссии", "status": "completed"},
    ))

    events = _jarvis_events(emitted)
    ends = [e for e in events if e[0] == "event:jarvis:end"]
    assert len(ends) == 1, f"пузырь закрыт {len(ends)} раз: {ends}"
    assert ends[0][1]["id"] == "T3", "correlation id финала должен совпадать с mission_id"
    assert "T3" not in server._streaming_started


def test_failed_streamed_mission_closes_bubble_once():
    server, emitted, _orch = _make_server()
    server._streaming_started.add("T4")
    from core.task_runtime import EVENT_TASK_FAILED

    server._on_task_event(_task_event(EVENT_TASK_FAILED, "T4", {"error": "boom"}))
    server._on_task_event(_task_event(EVENT_TASK_FAILED, "T4", {"error": "boom"}))

    ends = [e for e in _jarvis_events(emitted) if e[0] == "event:jarvis:end"]
    assert len(ends) == 1, f"end при failed отправлен {len(ends)} раз"
