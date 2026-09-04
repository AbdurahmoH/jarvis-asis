"""Regression: БАГ 2 — финальный текст миссии уходит ровно один раз.

До фикса: _mission_runner вызывал _output_callback(result_text), который
создавал пузырь start+end. Затем _on_task_event для EVENT_TASK_COMPLETED
создавал второй пузырь event:jarvis.

После фикса: mission.metadata["_output_sent"] = True устанавливается перед
_output_callback, и _on_task_event проверяет этот флаг.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest


def test_mission_metadata_output_sent_flag_set():
    """_mission_runner устанавливает _output_sent в metadata перед _output_callback."""
    src = open("core/orchestrator.py", encoding="utf-8").read()
    assert '"_output_sent"' in src or "'_output_sent'" in src, (
        "Флаг _output_sent не найден в orchestrator.py"
    )
    # Флаг должен устанавливаться ДО вызова _output_callback
    idx_flag = src.find('"_output_sent"')
    if idx_flag < 0:
        idx_flag = src.find("'_output_sent'")
    idx_callback = src.find("self._output_callback(result_text)")
    assert idx_flag < idx_callback, (
        "Флаг _output_sent должен устанавливаться ДО вызова _output_callback"
    )


def test_on_task_event_checks_output_sent_flag():
    """_on_task_event проверяет _output_sent перед созданием пузыря."""
    src = open("core/ws_server.py", encoding="utf-8").read()
    assert "_output_sent" in src, (
        "_output_sent не найден в ws_server.py"
    )
    assert "get_mission" in src, (
        "get_mission не вызывается в ws_server.py для проверки _output_sent"
    )


def test_output_sent_set_initialized():
    """JarvisWSServer инициализирует _output_sent как пустой set."""
    src = open("core/ws_server.py", encoding="utf-8").read()
    assert "self._output_sent: Set[str] = set()" in src, (
        "_output_sent не инициализирован в __init__"
    )


def test_mission_runner_sets_output_sent_before_callback():
    """Интеграционный тест: _mission_runner устанавливает флаг."""
    from core.task_runtime import Mission, MissionStatus, new_mission_id

    # Создаём минимальную миссию
    mission = Mission(task_id=new_mission_id(), goal="тест")

    output_called = []
    flag_before_callback = []

    def fake_output_callback(text: str) -> None:
        # Проверяем что флаг уже установлен к моменту вызова callback
        flag_before_callback.append(mission.metadata.get("_output_sent", False))
        output_called.append(text)

    # Патчим минимальный orchestrator
    from config.settings import Settings
    from unittest.mock import MagicMock, patch

    settings = Settings()

    with patch("core.orchestrator.Orchestrator.__init__", lambda self, *a, **kw: None):
        from core.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch._output_callback = fake_output_callback
        orch._session = None
        orch._memory = MagicMock()
        orch._memory.remember_exchange = MagicMock()
        orch._cognitive = MagicMock()
        orch._kernel = MagicMock()
        orch._agent = MagicMock()
        orch._agent.run_mission = MagicMock(return_value="результат теста")
        orch._settings = settings

        def fake_queue_output(output):
            return None
        orch._queue_assistant_output = fake_queue_output

        cancel = threading.Event()
        result = orch._mission_runner(mission, cancel)

    assert result == "результат теста"
    assert len(output_called) == 1, f"_output_callback вызван {len(output_called)} раз(а), ожидался 1"
    assert flag_before_callback == [True], (
        f"Флаг _output_sent должен быть True к моменту вызова callback, получено: {flag_before_callback}"
    )
    assert mission.metadata.get("_output_sent") is True
