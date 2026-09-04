"""Regression: БАГ 16 — play_music search_opened не запускает repair loop.

До фикса: verify_play_music возвращал verified=False без non_repeatable.
Repair loop запускался и повторно открывал музыку (двойной запуск).

После фикса: non_repeatable=True → repair loop не вызывается.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from core.actions.base import ActionResult
from core.verifier import verify_play_music, VerificationResult


def _make_search_opened_result() -> ActionResult:
    """ActionResult для play_music с stage=search_opened."""
    return ActionResult(
        tool="play_music",
        args={"query": "рок музыка", "source": "auto"},
        ok=True,
        output={
            "stage": "search_opened",
            "summary": "Открыт поиск по запросу: рок музыка",
        },
    )


def test_verify_play_music_search_opened_is_non_repeatable():
    """verify_play_music для search_opened возвращает non_repeatable=True."""
    result = _make_search_opened_result()
    verification = verify_play_music(result)

    assert verification.verified is False, "search_opened не должен быть verified"
    assert verification.non_repeatable is True, (
        "search_opened должен быть non_repeatable=True чтобы repair не повторял вызов"
    )
    assert "поисковая страница" in verification.detail.lower() or "воспроизведение" in verification.detail.lower()


def test_verify_play_music_spotify_query_is_non_repeatable():
    """verify_play_music для spotify+query возвращает non_repeatable=True."""
    result = ActionResult(
        tool="play_music",
        args={"query": "jazz", "source": "spotify"},
        ok=True,
        output={"stage": "service_opened"},
    )
    verification = verify_play_music(result)
    assert verification.non_repeatable is True


def test_verify_play_music_failure_is_repeatable():
    """verify_play_music для ok=False возвращает non_repeatable=False (можно retry)."""
    result = ActionResult(
        tool="play_music",
        args={"query": "test"},
        ok=False,
        error="медиаточка не открыта",
    )
    verification = verify_play_music(result)
    assert verification.verified is False
    assert verification.non_repeatable is False, (
        "Ошибка запуска должна быть repeatable — retry имеет смысл"
    )


def test_non_repeatable_field_in_to_dict():
    """VerificationResult.to_dict() включает non_repeatable."""
    vr = VerificationResult(
        verified=False,
        method="test",
        detail="test detail",
        non_repeatable=True,
    )
    d = vr.to_dict()
    assert "non_repeatable" in d
    assert d["non_repeatable"] is True


def test_agent_skips_repair_for_non_repeatable(tmp_path):
    """Agent не запускает repair для non_repeatable verification."""
    from core.verifier import VerificationResult

    # Проверяем что в agent.py есть проверка non_repeatable
    src = open("core/agent.py", encoding="utf-8").read()
    assert "non_repeatable" in src, (
        "agent.py не проверяет non_repeatable — repair loop будет запускаться"
    )
    assert "getattr(verification, \"non_repeatable\", False)" in src or \
           "verification.non_repeatable" in src, (
        "Проверка non_repeatable должна быть в agent.py"
    )
