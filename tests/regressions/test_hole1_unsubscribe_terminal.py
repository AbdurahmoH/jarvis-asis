"""Regression: ДЫРА 1 — подписчики TaskRuntime не растут при долгой работе.

До фикса unsubscribe вызывался только в happy-path _mission_runner.
При исключении в рантайме (FAILED) подписка оставалась навсегда.

Тест: 100 миссий вперемешку success/failure — после терминальных состояний
число подписчиков EventBus возвращается к нулю.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_orchestrator():
    from config.settings import Settings
    from core.orchestrator import Orchestrator
    from core.task_runtime import TaskRuntime

    settings = Settings()
    runtime = TaskRuntime(max_concurrent=2)

    orch = Orchestrator.__new__(Orchestrator)
    orch._running = True
    orch._settings = settings
    orch._proactor = MagicMock()
    orch._kernel = MagicMock()
    orch._kernel.submit.return_value = MagicMock(id="k1", task_id="kt1")
    orch._kernel.ledger.load.return_value = None
    orch._intake = MagicMock()
    orch._intake.classify.return_value.to_dict.return_value = {}
    orch._runtime = runtime
    orch._agent = MagicMock()
    orch._cognitive = MagicMock()
    orch._memory = MagicMock()
    orch._session = None
    orch._output_callback = lambda text: None
    orch._queue_assistant_output = lambda output: None
    return orch, runtime


def test_subscriber_count_does_not_grow_over_100_missions():
    orch, runtime = _make_orchestrator()

    # Половина миссий падает в рантайме — раньше это оставляло подписки.
    outcomes = ["ок"] * 50 + [RuntimeError("worker boom")] * 50
    outcomes.reverse()

    def run_mission(mission, cancel):
        item = outcomes.pop()
        if isinstance(item, Exception):
            raise item
        return item

    orch._agent.run_mission.side_effect = run_mission

    baseline = len(runtime._bus._subscribers)
    missions = []
    for i in range(100):
        missions.append(orch.submit_goal(f"задача номер {i}", on_event=lambda e: None))
    assert len(missions) == 100

    for mission in missions:
        done = runtime.wait(mission.task_id, timeout=15.0)
        assert done is not None and done.status.is_terminal, (
            f"миссия {mission.task_id} не достигла терминального состояния"
        )

    # События доставляются из рабочих потоков — даём отпискам исполниться.
    import time

    time.sleep(0.3)
    assert len(runtime._bus._subscribers) == baseline, (
        f"подписчики утекли: {len(runtime._bus._subscribers) - baseline} висячих "
        "подписок после 100 терминальных миссий"
    )
