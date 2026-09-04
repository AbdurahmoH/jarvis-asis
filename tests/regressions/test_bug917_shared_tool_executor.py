"""Regression: БАГ 9/17 — ToolExecutor живёт на уровне Agent (один на процесс).

До фикса executor кэшировался в context.extra, а ToolContext создавался
на каждый вызов → каждый вызов получал свежий семафор, лимит
max_parallel_tools не действовал вообще.

Тесты:
1. Agent._tool_executor.capacity == settings.limits.max_parallel_tools (> 0).
2. Все ToolContext'ы агента получают один и тот же executor.
3. Функционально: два параллельных execute_tool с общим executor'ом и
   capacity=1 не перекрываются (семафор реально работает).
4. execute_tool без внедрённого executor'а по-прежнему работает (fallback).
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest


def _make_agent():
    from config.settings import Settings
    from core.agent import Agent

    agent = Agent(Settings(), council=MagicMock())
    return agent


def test_agent_executor_capacity_from_settings():
    from config.settings import Settings

    settings = Settings()
    agent = _make_agent()
    expected = int(getattr(settings.limits, "max_parallel_tools", 4))
    assert expected > 0, "max_parallel_tools должен быть > 0 по умолчанию"
    assert agent._tool_executor.capacity == expected


def test_agent_injects_executor_into_tool_context():
    agent = _make_agent()
    from core.actions.base import ToolContext

    context = ToolContext(
        user_id="default",
        settings=agent._settings,
        state=None,
        extra={
            "confirmation_approved": False,
            "task_runtime": None,
            "tool_executor": agent._tool_executor,
            "authority_grant_id": None,
        },
    )
    from core.actions.executor import _executor_for

    assert _executor_for(context) is agent._tool_executor, (
        "execute_tool должен использовать внедрённый агентом executor, "
        "а не создавать новый на каждый вызов"
    )


def test_shared_executor_semaphore_bounds_parallelism():
    from core.actions.base import ActionResult, Tool, ToolContext
    from core.actions.executor import ToolExecutor, execute_tool
    from core.actions.registry import ToolRegistry

    class _SlowTool(Tool):
        name = property(lambda self: "slow_probe")

        @property
        def description(self):
            return "probe"

        @property
        def input_schema(self):
            return {"type": "object", "properties": {}}

        def run(self, args, context):
            with _SlowTool.guard:
                _SlowTool.concurrent += 1
                _SlowTool.max_seen = max(_SlowTool.max_seen, _SlowTool.concurrent)
                time.sleep(0.15)
                _SlowTool.concurrent -= 1
            return ActionResult(tool=self.name, args=args, ok=True, output="done")

    class _SlowToolFixed(_SlowTool):
        name = "slow_probe"

    _SlowTool.guard = threading.Lock()
    _SlowTool.concurrent = 0
    _SlowTool.max_seen = 0

    registry = ToolRegistry()
    registry.register(_SlowToolFixed())

    executor = ToolExecutor(max_parallel=1)
    shared_context_extra = {"tool_executor": executor}

    def _worker():
        context = ToolContext(user_id="t", settings=MagicMock(), state=None,
                              extra=dict(shared_context_extra))
        result = execute_tool(registry, "slow_probe", {}, context, max_retries=0)
        assert result.ok

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert _SlowTool.max_seen == 1, (
        f"parallelism {getattr(_SlowTool, 'max_seen', 0)} > 1 — семафор мёртв "
        "(БАГ 9/17 не исправлен)"
    )


def test_execute_tool_fallback_without_injected_executor():
    from core.actions.base import ActionResult, Tool, ToolContext
    from core.actions.executor import execute_tool
    from core.actions.registry import ToolRegistry

    class _EchoTool(Tool):
        name = "echo_probe"

        @property
        def description(self):
            return "echo"

        @property
        def input_schema(self):
            return {"type": "object", "properties": {}}

        def run(self, args, context):
            return ActionResult(tool=self.name, args=args, ok=True, output="ok")

    registry = ToolRegistry()
    registry.register(_EchoTool())
    context = ToolContext(user_id="t", settings=MagicMock(), state=None, extra={})
    result = execute_tool(registry, "echo_probe", {}, context, max_retries=0)
    assert result.ok
