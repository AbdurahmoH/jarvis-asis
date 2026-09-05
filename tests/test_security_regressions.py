from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from config.settings import Settings
from core.actions.base import ToolContext
from core.actions.executor import tool_timeout_for
from core.actions.browser_automation import (
    BrowserAutomationEngine,
    BrowserAutomationError,
)
from core.actions.browser_bridge import BrowserBridgeTool
from core.platform.browser import BrowserAutomationProvider
from core.platform.browser_bridge import BrowserBridge
from core.ws_server import JarvisWSServer


def test_browser_open_rejects_case_variant_and_non_http_urls() -> None:
    engine = BrowserAutomationEngine()

    with pytest.raises(BrowserAutomationError, match="URL заблокирован"):
        engine.open("HTTP://127.0.0.1:8782/")
    with pytest.raises(BrowserAutomationError, match="file URL"):
        engine.open("file:///C:/Windows/System32/drivers/etc/hosts")


def test_browser_navigate_revalidates_case_variant_internal_url() -> None:
    class Page:
        def goto(self, *_args, **_kwargs):
            raise AssertionError("navigation must be blocked before goto")

    class Engine:
        _page = Page()

    with pytest.raises(ValueError, match="URL заблокирован"):
        BrowserAutomationProvider(Engine()).navigate("hTtP://127.0.0.1:8782/")


def test_ws_cloud_endpoint_rejects_private_networks() -> None:
    with pytest.raises(ValueError, match="URL заблокирован"):
        JarvisWSServer._validate_base_url("http://127.0.0.1:8782/v1")


def test_browser_download_directory_is_confined(tmp_path: Path) -> None:
    settings = Settings()
    settings.paths.documents_dir = str(tmp_path / "documents")
    context = ToolContext(settings=settings)

    safe = BrowserBridgeTool._download_directory(context, "reports")
    assert safe == (tmp_path / "documents" / "downloads" / "reports").resolve()

    with pytest.raises(ValueError, match="must stay inside"):
        BrowserBridgeTool._download_directory(context, "../../outside")


def test_browser_bridge_tool_keeps_playwright_on_one_worker_thread() -> None:
    class ThreadRecordingBridge(BrowserBridge):
        def __init__(self) -> None:
            self.thread_ids: list[int] = []

        def open(self, url: str):
            self.thread_ids.append(threading.get_ident())
            return {"ok": True, "url": url}

        def observe(self):
            self.thread_ids.append(threading.get_ident())
            return {"ok": True}

    bridge = ThreadRecordingBridge()
    tool = BrowserBridgeTool(bridge=bridge)
    context = ToolContext()

    assert tool.run({"action": "open", "url": "https://example.com"}, context).ok
    assert tool.run({"action": "observe"}, context).ok
    assert len(bridge.thread_ids) == 2
    assert len(set(bridge.thread_ids)) == 1
    assert bridge.thread_ids[0] != threading.get_ident()


def test_browser_tools_use_browser_timeout_budget() -> None:
    """S4: браузер и CUA — свой класс таймаута, не файловый и не веб.

    Тест переписан в рамках S4 сознательно. Его смысл — «браузерные
    инструменты не должны получать короткий файловый бюджет» — сохранён и
    усилен: добавлен computer_screenshot (раньше он как раз падал в файловый
    класс) и закреплено, что веб-класс остался ОТДЕЛЬНЫМ значением, то есть
    бюджет браузера больше не выводится из соседнего класса.
    """
    settings = Settings()
    settings.limits.tool_timeout_file_sec = 2.0
    settings.limits.tool_timeout_web_sec = 31.0
    settings.limits.tool_timeout_browser_sec = 61.0
    context = ToolContext(settings=settings)

    assert tool_timeout_for("browser_bridge", context) == 61.0
    assert tool_timeout_for("browser_automation", context) == 61.0
    assert tool_timeout_for("computer_screenshot", context) == 61.0
    assert tool_timeout_for("web_fetch", context) == 31.0


def test_packaged_runtime_config_is_deepseek_and_silent() -> None:
    path = Path(
        "jarvis/src-tauri/resources/jarvis-runtime/config/settings.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["offline_mode"] is False
    assert data["deepseek_brain_mode"] is True
    expected_providers = {
        key: "deepinfra"
        for key in ("fast", "analyst", "coder", "architect", "research")
    }
    assert data["tier_providers"] == expected_providers
    assert data["launcher"]["greeting_enabled"] is False
