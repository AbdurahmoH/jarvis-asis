"""Сбой модели ≠ «не умею»: быстрая честная ошибка, БЕЗ черновика навыка.

Регрессия на живой баг (см. лог ws_server): при таймауте/401 провайдера
агент создавал черновик навыка с искажённым именем и отвечал
«Готового способа для этой задачи у меня пока нет…» с сырыми HTTP-кодами.

Ожидаемое поведение после фикса:
  * сбой модели -> короткая дружелюбная фраза (MODEL_UNAVAILABLE_TEXT),
    без HTTP-кодов/трейсбеков в тексте для пользователя;
  * SkillForge черновик НЕ создаётся (навыки — только для реальных
    «не умею», не для сетевых сбоев);
  * FAST-тир получает короткую политику таймаута/попыток
    (limits.fast_tier_timeout_sec / fast_tier_max_retries).
"""

from __future__ import annotations

import pytest

from config.settings import LOCAL_PROVIDER, Settings
from core.agent import Agent, AgentConfig, MODEL_UNAVAILABLE_TEXT
from core.llm import Tier
from core.llm.backend import BackendUnavailable
from core.llm import factory as llm_factory


class DeadBackendMixin:
    """Бэкенд, у которого ВСЕ вызовы chat падают по недоступности провайдера."""

    def chat(self, messages, system=None, max_tokens=None, temperature=None) -> str:
        raise BackendUnavailable(
            "Провайдер anymodel недоступен: исчерпаны 2 попыток. "
            "Последняя ошибка: HTTP 408: таймаут 7.0 с"
        )


def _make_dead_backend(base):
    class DeadBackend(DeadBackendMixin, base):
        pass
    return DeadBackend()


@pytest.fixture
def dead_backend(monkeypatch):
    """Подменяет get_llm_backend «мёртвым» бэкендом (провайдер лёг)."""
    from tests.conftest import FakeBackend
    from core import llm as llm_mod

    backend = _make_dead_backend(FakeBackend)

    def _fake_get(settings, tier=Tier.FAST, *, policy_override=None):
        return backend

    monkeypatch.setattr(llm_mod, "get_llm_backend", _fake_get)
    import core.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(Settings, "is_tier_available", lambda self, tier: True)
    return backend


def test_model_failure_friendly_text_no_draft(dead_backend, settings, tmp_path):
    """Сбой модели -> MODEL_UNAVAILABLE_TEXT, mode=model_error, БЕЗ черновика."""
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=True))
    # Изолируем хранилище навыков во временную папку.
    assert agent._forge is not None
    agent._forge._dir = tmp_path

    outcome = agent.execute("напиши большой текст про закат на море")

    assert outcome.mode == "model_error"
    # C4 (cloud-only): текст — фраза маппера (transient: таймаут провайдера);
    # raw-причина («HTTP 408», имена провайдеров) ушла в trace/логи.
    from core.brain.error_messages import MESSAGES
    assert outcome.text == MESSAGES["transient"]
    lowered = outcome.text.lower()
    assert "http 408" not in lowered
    assert "таймаут" not in lowered
    assert "anymodel" not in lowered
    assert "навык" not in lowered
    assert "не научен" not in lowered
    assert "готового способа" not in lowered
    # Черновик навыка НЕ создан.
    assert list(tmp_path.glob("*.md")) == []


def test_model_failure_text_safe_for_tts():
    """Фраза сбоя проходит TTS-санитайзер как есть (не подменяется fallback)."""
    from core.voice.tts_sanitizer import looks_unsafe_for_tts

    assert not looks_unsafe_for_tts(f"{MODEL_UNAVAILABLE_TEXT} BackendUnavailable: llama-server не найден")


def test_fast_tier_short_retry_policy():
    """FAST-тир строится с коротким таймаутом и минимумом попыток."""
    from core.llm.backend import BackendConfigError

    from config import load_config
    real = load_config()  # конфиг проекта (не офлайн-фикстура без ключей)
    llm_factory.clear_backend_cache()
    try:
        try:
            backend = llm_factory.get_llm_backend(real, Tier.FAST)
        except BackendConfigError:
            pytest.skip("FAST-тир не настроен на внешнего провайдера в этой конфигурации")
        if not hasattr(backend, "_timeout"):
            pytest.skip("FAST-тир настроен на локальную модель — политика не применяется")
        assert backend._timeout == real.limits.fast_tier_timeout_sec
        assert backend._max_retries == real.limits.fast_tier_max_retries
    finally:
        llm_factory.clear_backend_cache()


def test_analyst_tier_keeps_full_policy():
    """Аналитический тир остаётся на общих лимитах (глубокая работа)."""
    from core.llm.backend import BackendConfigError

    from config import load_config
    real = load_config()
    llm_factory.clear_backend_cache()
    try:
        try:
            backend = llm_factory.get_llm_backend(real, Tier.ANALYST)
        except BackendConfigError:
            pytest.skip("ANALYST-тир не настроен на внешнего провайдера в этой конфигурации")
        if not hasattr(backend, "_timeout"):
            pytest.skip("ANALYST-тир настроен на локальную модель — политика не применяется")
        assert backend._timeout == real.limits.response_timeout_sec
        assert backend._max_retries == real.limits.max_retries
    finally:
        llm_factory.clear_backend_cache()
