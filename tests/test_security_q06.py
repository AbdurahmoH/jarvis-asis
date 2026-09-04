"""Regression tests for production HANDS computer-use wiring."""

from pathlib import Path

import pytest

from config.settings import Settings
from core.actions.base import ToolContext
from core.actions.computer_use import (
    ComputerKeyboardTool,
    ComputerMouseTool,
    ComputerScreenshotTool,
    _BACKEND,
)
from core.verifier import has_strict_verifier, verify_action_result


def _has_interactive_desktop() -> bool:
    try:
        import mss
        mss_cls = getattr(mss, "MSS", mss.mss)
        with mss_cls() as sct:
            monitor = sct.monitors[0]
            region = {"top": monitor["top"], "left": monitor["left"], "width": 1, "height": 1}
            sct.grab(region)
            return True
    except Exception:
        return False


def test_computer_use_tools_registered_with_strict_verifiers():
    from core.actions import DEFAULT_REGISTRY

    for name in ("computer_mouse", "computer_keyboard", "computer_screenshot"):
        assert DEFAULT_REGISTRY.get(name) is not None
        assert has_strict_verifier(name)


def test_production_adapter_uses_real_backend():
    assert _BACKEND.is_real is True
    assert "Physically" in ComputerMouseTool().description
    assert "Physically" in ComputerKeyboardTool().description


@pytest.mark.skipif(
    not _has_interactive_desktop(),
    reason="Physical desktop capture unavailable (BitBlt failed in non-interactive/headless session)",
)
def test_screenshot_is_physical_and_verified(tmp_path):
    settings = Settings()
    settings.paths.documents_dir = str(tmp_path)
    result = ComputerScreenshotTool().run(
        {"path": "hands/test-screen.png"}, ToolContext(settings=settings),
    )
    assert result.ok is True
    assert result.output["physical"] is True
    assert Path(result.output["path"]).is_file()
    verification = verify_action_result(result)
    assert verification.verified is True
    assert verification.strict is True


def test_keyboard_rejects_unbound_physical_action():
    result = ComputerKeyboardTool().run(
        {"action": "focus_window"}, ToolContext(settings=Settings()),
    )
    assert result.ok is False
    assert "window_title" in result.error
