"""Explicit media launcher; never silently turns a media request into a reminder."""

from __future__ import annotations

import ctypes
import os
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.actions.web_search import duckduckgo_search

__all__ = ["PlayMusicTool", "play_music"]


def _open_target(target: str, *, source: str) -> bool:
    if os.name == "nt" and hasattr(os, "startfile"):
        os.startfile(target)  # type: ignore[attr-defined]
        return True
    return bool(webbrowser.open(target))


def _open_default_player() -> bool:
    """Open the locally associated music player without a network lookup.

    A bare "поставь музыку" is a command to bring up the local playback
    surface, not a request to invent a track or create a reminder.  The
    Windows URI is handled by the installed Media Player association; the
    browser fallback keeps the tool testable on non-Windows hosts.
    """
    targets = ("mswindowsmusic:", "mswindowsmusic://") if os.name == "nt" else ("music:",)
    for target in targets:
        try:
            if _open_target(target, source="local"):
                return True
        except (OSError, ValueError):
            continue
    return False


def _request_playback() -> bool:
    """Send the Windows media PLAY command without toggling active audio off."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd_broadcast = 0xFFFF
        wm_appcommand = 0x0319
        appcommand_media_play = 46
        user32.SendMessageW(
            hwnd_broadcast,
            wm_appcommand,
            0,
            appcommand_media_play << 16,
        )
        return True
    except Exception:
        return False


def play_music(*, query: str = "", mood: str = "", uri: str = "", path: str = "",
               source: str = "auto", allow_network: bool = False) -> ActionResult:
    args = {"query": query, "mood": mood, "uri": uri, "path": path,
            "source": source, "allow_network": allow_network}
    target = (path or uri or "").strip()
    if target:
        if path and not Path(path).expanduser().exists():
            return ActionResult(tool="play_music", args=args, ok=False,
                                error=f"Музыкальный файл не найден: {path}")
        try:
            opened = _open_target(str(Path(path).expanduser()) if path else target, source=source)
            if not opened:
                return ActionResult(tool="play_music", args=args, ok=False, error="Медиаточка не принята системой")
            return ActionResult(tool="play_music", args=args, ok=True,
                                output=f"Открыл медиаточку: {target}", side_effects_contained=False)
        except (OSError, ValueError) as exc:
            return ActionResult(tool="play_music", args=args, ok=False, error=f"Не удалось открыть медиаточку: {exc}")

    # A mood is descriptive context for the local player, not an implicit
    # search query. Only an explicitly network-enabled caller may turn it
    # into text for a remote provider; bare voice commands stay local.
    query = " ".join((query or (mood if allow_network else "")).split())
    if not query:
        if _open_default_player():
            requested = _request_playback()
            return ActionResult(
                tool="play_music", args=args, ok=True,
                output=(
                    "Открыл локальный музыкальный плеер и отправил команду воспроизведения."
                    if requested else
                    "Открыл локальный музыкальный плеер; команда воспроизведения не подтверждена."
                ),
                side_effects_contained=False,
            )
        return ActionResult(tool="play_music", args=args, ok=False,
                            error="Локальный музыкальный плеер не найден")
    source = (source or "auto").casefold()
    if not allow_network:
        return ActionResult(tool="play_music", args=args, ok=False,
                            error="Для поиска трека в сети нужно явно разрешить сетевой источник")

    if source == "auto":
        return ActionResult(
            tool="play_music", args=args, ok=False,
            error="Для сетевого поиска укажите конкретный источник: youtube или spotify",
        )
    if source == "spotify":
        target = "spotify:search:" + urllib.parse.quote(query)
    else:
        results = duckduckgo_search(f"site:youtube.com/watch {query}", max_results=5)
        direct = next((
            str(item.get("url") or "") for item in results
            if "youtube.com/watch" in str(item.get("url") or "")
            or "youtu.be/" in str(item.get("url") or "")
        ), "")
        target = direct or ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
    try:
        opened = _open_target(target, source=source)
        if not opened:
            return ActionResult(tool="play_music", args=args, ok=False, error="Медиасервис не открылся")
        if source == "youtube":
            stage = "video_opened" if "youtube.com/watch" in target or "youtu.be/" in target else "search_opened"
            return ActionResult(
                tool="play_music", args=args, ok=True,
                output={"stage": stage, "query": query, "url": target},
                side_effects_contained=False,
            )
        return ActionResult(
            tool="play_music", args=args, ok=True,
            output={"stage": "service_opened", "query": query, "uri": target},
            side_effects_contained=False,
        )
    except (OSError, ValueError) as exc:
        return ActionResult(tool="play_music", args=args, ok=False, error=f"Не удалось открыть медиасервис: {exc}")


class PlayMusicTool(Tool):
    @property
    def name(self) -> str:
        return "play_music"

    @property
    def description(self) -> str:
        return (
            "Запускает указанную музыку через локальный путь/URI или выбранный "
            "источник. Для поиска по исполнителю или названию передай query, "
            "source=spotify либо youtube и allow_network=true. Результат поиска "
            "сам по себе не считается подтверждённым воспроизведением."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "mood": {"type": "string", "default": ""},
                "uri": {"type": "string", "default": ""},
                "path": {"type": "string", "default": ""},
                "source": {"type": "string", "enum": ["auto", "spotify", "youtube", "local"], "default": "auto"},
                "allow_network": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        return play_music(query=args.get("query", ""), mood=args.get("mood", ""),
                          uri=args.get("uri", ""), path=args.get("path", ""),
                          source=args.get("source", "auto"),
                          allow_network=bool(args.get("allow_network", False)))


DEFAULT_REGISTRY.register(PlayMusicTool())
