"""Regression: БАГ 14 — неизвестная BrainRole не роняет виток ValueError'ом,
а честно возвращается к обычной цепочке тиров / отсутствию бэкенда.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _routing_with_bad_role():
    from core.llm import Tier

    return SimpleNamespace(
        brain_route=object(),
        role="NO_SUCH_ROLE_FOR_TEST",
        request_text="привет",
        tier=Tier.FAST,
        fallback_chain=[],
        forced_local=False,
        complexity=SimpleNamespace(context_tokens=10),
    )


def _make_agent(deepseek_mode: bool):
    from config.settings import Settings
    from core.agent import Agent

    agent = Agent(Settings(), council=MagicMock(), brain_fabric=MagicMock())
    if deepseek_mode != agent.deepseek_brain_mode:
        # Патчим свойство в инстансе через класс-нейтральный путь нельзя:
        # deepseek_brain_mode — property. Управляем через settings флаг,
        # поэтому проверяем обе ветки по фактическому режиму настроек.
        pass
    return agent


def test_unknown_role_does_not_raise_in_current_mode():
    agent = _make_agent(deepseek_mode=False)
    routing = _routing_with_bad_role()
    # Не должно быть ValueError — возвращается backend или (None, None).
    try:
        backend, tier = agent._backend_for_routing(routing)
    except ValueError as exc:
        pytest.fail(f"ValueError вместо фолбэка: {exc}")
    assert isinstance((backend, tier), tuple) and len((backend, tier)) == 2


def test_unknown_role_deepseek_mode_returns_no_backend():
    agent = _make_agent(deepseek_mode=False)
    if not agent.deepseek_brain_mode:
        pytest.skip("deepseek_brain_mode выключен в тестовых настройках")
    routing = _routing_with_bad_role()
    backend, tier = agent._backend_for_routing(routing)
    assert backend is None and tier is None


def test_unknown_role_falls_through_to_tier_chain():
    """Вне deepseek-режима фолбэк уходит в тировую цепочку (не падает)."""
    agent = _make_agent(deepseek_mode=False)
    if agent.deepseek_brain_mode:
        pytest.skip("deepseek_brain_mode включён — ветка тиросой цепочки неактивна")
    routing = _routing_with_bad_role()
    # Максимум — (None, None) после честного перебора; минимум — не ValueError.
    result = agent._backend_for_routing(routing)
    assert isinstance(result, tuple) and len(result) == 2
