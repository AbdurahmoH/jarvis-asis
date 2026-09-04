"""Production adapters for the existing real Windows CUA backend."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.cua import Point, build_backend
from core.platform.windows import NativeWindowsProvider

__all__ = [
    "DryRunInputController",
    "ComputerMouseTool",
    "ComputerKeyboardTool",
    "ComputerScreenshotTool",
]


@dataclass
class DryRunInputController:
    """Compatibility recorder for callers that explicitly request a dry run."""

    actions: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"kind": kind, "args": dict(args or {}), "ts": time.time(), "mode": "dry_run"}
        self.actions.append(entry)
        return entry

    def clear(self) -> None:
        self.actions.clear()


_CONTROLLER = DryRunInputController()
_BACKEND = build_backend(real_input=True)
_WINDOWS = NativeWindowsProvider()


def _active_window() -> Dict[str, Any]:
    try:
        return dict(_WINDOWS.window_active())
    except Exception as exc:
        return {"title": "", "error": f"{type(exc).__name__}: {exc}"}


def _screen_fingerprint() -> str:
    try:
        import mss
        with mss.mss() as capture:
            frame = capture.grab(capture.monitors[0])
            return hashlib.sha256(bytes(frame.rgb)).hexdigest()
    except Exception:
        return ""


def _observed_action(tool: str, args: Dict[str, Any], execute) -> ActionResult:
    before_window = _active_window()
    before_screen = _screen_fingerprint()
    try:
        backend_result = execute()
    except Exception as exc:
        return ActionResult(tool, args, False, error=f"{type(exc).__name__}: {exc}")
    if isinstance(backend_result, dict) and not backend_result.get("ok", True):
        return ActionResult(tool, args, False, error=str(backend_result.get("error") or backend_result))
    time.sleep(0.35)
    after_window = _active_window()
    after_screen = _screen_fingerprint()
    changed = bool(before_screen and after_screen and before_screen != after_screen)
    focused = before_window.get("handle") != after_window.get("handle")
    output = {
        "physical": bool(_BACKEND.is_real),
        "backend_result": backend_result,
        "active_before": before_window,
        "active_after": after_window,
        "screen_changed": changed,
        "observed": bool(focused or changed),
    }
    return ActionResult(tool, args, bool(_BACKEND.is_real), output=output,
                        error=None if _BACKEND.is_real else "real CUA backend is inactive")


class ComputerMouseTool(Tool):
    @property
    def name(self) -> str:
        return "computer_mouse"

    @property
    def description(self) -> str:
        return (
            "Physically controls the Windows mouse through the existing real CUA backend. "
            "Coordinates are normalized from 0 to 1000. Use only after observing the screen."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["move", "click", "double_click", "right_click"]},
                "x": {"type": "number", "minimum": 0, "maximum": 1000},
                "y": {"type": "number", "minimum": 0, "maximum": 1000},
            },
            "required": ["action", "x", "y"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        point = Point(float(args["x"]), float(args["y"]))
        return _observed_action(self.name, args, lambda: _BACKEND.act(str(args["action"]), point=point))


class ComputerKeyboardTool(Tool):
    @property
    def name(self) -> str:
        return "computer_keyboard"

    @property
    def description(self) -> str:
        return (
            "Physically types, presses keys, sends hotkeys, or focuses a real Windows window. "
            "For focus_window provide window_title; for hotkey provide keys such as alt+tab."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["type", "press", "hotkey", "focus_window"]},
                "text": {"type": "string"},
                "key": {"type": "string"},
                "keys": {"type": "string"},
                "window_title": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        action = str(args["action"])
        if action == "focus_window":
            title = str(args.get("window_title") or "").strip()
            if not title:
                return ActionResult(self.name, args, False, error="focus_window requires window_title")
            return _observed_action(self.name, args, lambda: _WINDOWS.window_focus(title=title))
        if action == "type":
            return _observed_action(self.name, args, lambda: _BACKEND.act("type", text=str(args.get("text") or "")))
        if action == "press":
            key = str(args.get("key") or "").strip()
            if not key:
                return ActionResult(self.name, args, False, error="press requires key")
            return _observed_action(self.name, args, lambda: _BACKEND.act("press", text=key))
        keys = str(args.get("keys") or "").strip()
        if not keys:
            return ActionResult(self.name, args, False, error="hotkey requires keys")
        return _observed_action(self.name, args, lambda: _BACKEND.act("hotkey", text=keys))


class ComputerScreenshotTool(Tool):
    @property
    def name(self) -> str:
        return "computer_screenshot"

    @property
    def description(self) -> str:
        return "Captures the physical Windows desktop to a PNG inside documents_dir and reports the active window."

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "computer_captures/screen.png"}},
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        root = Path(context.settings.documents_dir).expanduser().resolve()
        requested = Path(str(args.get("path") or "computer_captures/screen.png"))
        target = (root / requested).resolve()
        if target != root and root not in target.parents:
            return ActionResult(self.name, args, False, error="screenshot path escapes documents_dir")
        try:
            import mss
            import mss.tools
            target.parent.mkdir(parents=True, exist_ok=True)
            mss_cls = getattr(mss, "MSS", mss.mss)
            with mss_cls() as capture:
                frame = capture.grab(capture.monitors[0])
                mss.tools.to_png(frame.rgb, frame.size, output=str(target))
            ok = target.is_file() and target.stat().st_size > 100
            return ActionResult(
                self.name, args, ok,
                output={"path": str(target), "bytes": target.stat().st_size if ok else 0,
                        "active_window": _active_window(), "physical": True},
                error=None if ok else "physical screenshot was not created",
            )
        except Exception as exc:
            return ActionResult(self.name, args, False, error=f"{type(exc).__name__}: {exc}")


DEFAULT_REGISTRY.register(ComputerMouseTool())
DEFAULT_REGISTRY.register(ComputerKeyboardTool())
DEFAULT_REGISTRY.register(ComputerScreenshotTool())
