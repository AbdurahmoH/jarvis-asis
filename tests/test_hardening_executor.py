from __future__ import annotations

import threading
import time
from typing import Any, Dict

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.executor import ToolExecutor, execute_tool
from core.actions.registry import ToolRegistry
from config.settings import Settings


class _ModuleLevelTimeoutTool(Tool):
    supports_hard_cancellation = True
    @property
    def name(self) -> str:
        return "hardening_timeout_tool"

    @property
    def description(self) -> str:
        return "writes only while alive"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, args, context):
        path = args["path"]
        while True:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("x\n")
                stream.flush()
            time.sleep(0.01)


def test_timeout_terminates_process_and_stops_post_timeout_writes(tmp_path) -> None:
    settings = Settings()
    registry = ToolRegistry()
    registry.register(_ModuleLevelTimeoutTool())
    path = tmp_path / "writes.log"
    result = execute_tool(
        registry, "hardening_timeout_tool", {"path": str(path)},
        ToolContext(settings=settings), timeout_sec=0.25,
    )
    before = path.stat().st_size if path.exists() else 0
    time.sleep(0.2)
    after = path.stat().st_size if path.exists() else 0
    assert not result.ok
    assert result.terminated is True
    # S4: процесс действительно убит — побочные эффекты локализованы.
    assert result.side_effects_contained is True
    assert after == before


def test_executor_semaphore_is_instance_scoped() -> None:
    first = ToolExecutor(1)
    second = ToolExecutor(1)
    assert first.semaphore is not second.semaphore
