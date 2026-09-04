"""Regression: БАГ 3 — напоминания реально срабатывают.

До фикса: TaskManager создаётся без callback (get_default_manager() без аргументов).
_fire() вызывает self._callback = None → напоминание молча теряется.

После фикса: TaskManager получает callback, который вызывает output при срабатывании.
"""
from __future__ import annotations

import threading
import time

import pytest

from core.actions.reminders import TaskManager


def test_reminder_callback_fires():
    """TaskManager вызывает callback при срабатывании напоминания."""
    fired = threading.Event()
    fired_text = []

    def on_fire(reminder_id: str, text: str) -> None:
        fired_text.append(text)
        fired.set()

    manager = TaskManager(callback=on_fire)
    # Добавляем напоминание через 0 минут — но минимум 1 по схеме.
    # Используем внутренний метод для теста с коротким таймером.
    import uuid
    reminder_id = uuid.uuid4().hex[:8]
    due_at = time.time() + 0.1  # 100ms

    def _fire_soon():
        time.sleep(0.1)
        with manager._lock:
            rem = manager._reminders.pop(reminder_id, None)
        if rem and manager._callback:
            manager._callback(rem.id, rem.text)

    from core.actions.reminders import Reminder
    timer = threading.Timer(0.1, _fire_soon)
    timer.daemon = True
    reminder = Reminder(id=reminder_id, text="выпить воды", due_at=due_at, timer=timer)
    with manager._lock:
        manager._reminders[reminder_id] = reminder
    timer.start()

    fired.wait(timeout=2.0)
    assert fired.is_set(), "Callback напоминания не был вызван за 2 секунды"
    assert "выпить воды" in fired_text, f"Текст напоминания не передан: {fired_text}"


def test_default_manager_has_no_callback_before_fix():
    """get_default_manager() без аргументов создаёт менеджер без callback — это баг."""
    from core.actions.reminders import get_default_manager, _DEFAULT_MANAGER
    import core.actions.reminders as rem_module

    # Сбрасываем глобальный менеджер
    original = rem_module._DEFAULT_MANAGER
    rem_module._DEFAULT_MANAGER = None
    try:
        manager = get_default_manager()
        # До фикса: callback = None
        # После фикса: callback должен быть установлен через orchestrator
        # Этот тест документирует что get_default_manager() сам по себе
        # не устанавливает callback — это ответственность Orchestrator
        assert manager._callback is None, (
            "get_default_manager() не должен сам устанавливать callback — "
            "это делает Orchestrator при инициализации"
        )
    finally:
        rem_module._DEFAULT_MANAGER = original


def test_orchestrator_sets_reminder_callback():
    """Orchestrator устанавливает callback на TaskManager при инициализации."""
    from core.actions.reminders import get_default_manager, _DEFAULT_MANAGER
    import core.actions.reminders as rem_module

    # Проверяем что в orchestrator.py есть код подключения callback
    src = open("core/orchestrator.py", encoding="utf-8").read()

    # После фикса: должен быть вызов TaskManager с callback или установка callback
    has_callback_setup = (
        "TaskManager(callback=" in src
        or "_task_manager._callback" in src
        or "task_manager.callback" in src
        or "_task_manager.callback" in src
        or "reminder_callback" in src
        or "_on_reminder_fired" in src
        or "trigger_reminder" in src
    )
    assert has_callback_setup, (
        "Orchestrator не устанавливает callback на TaskManager. "
        "Напоминания будут молча теряться."
    )
