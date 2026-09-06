"""Regression C4 — ни одного raw-исключения в ответе пользователю.

Аудит 2026-09-06 (provider broken): ``ProviderUnavailable('all routed
providers unavailable…')`` и ``NoRouteAvailable('no healthy provider
satisfies role=PLANNER…')`` попадали в ответ как текст; в cloud mode утекала
цепочка ``Ошибка DeepInfra/DeepSeek runtime: capability discovery failed:
DeepSeek discovery error: BackendUnavailable: …``. Наряд: raw — только в
trace/логи, пользователю — фразы из ``core.brain.error_messages``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.brain.error_messages import (
    FORBIDDEN_IN_OUTPUT,
    MESSAGES,
    classify,
    user_message_for,
)
from core.brain.models import ProviderUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Дословные сырые строки из артефактов аудита
#: (artifacts/phrase_run_offline.jsonl, phrase_run_online.jsonl).
AUDIT_RAW_ERRORS = [
    "ProviderUnavailable('all routed providers unavailable (deepinfra:ProviderUnavailable)')",
    "NoRouteAvailable('no healthy provider satisfies role=PLANNER, privacy=PERSONAL')",
    "DeepSeek runtime unavailable",
    ("capability discovery failed: DeepSeek discovery error: BackendUnavailable: "
     "all routed providers unavailable (deepinfra:ProviderUnavailable)"),
    "provider deepinfra credential is unavailable",
    "Ошибка DeepInfra/DeepSeek runtime: backend вернул пустой ответ.",
]


@pytest.mark.parametrize("raw", AUDIT_RAW_ERRORS, ids=lambda s: s[:40])
def test_audit_raw_errors_map_to_friendly_text(raw: str) -> None:
    message = user_message_for(raw)
    assert message in MESSAGES.values(), "фраза из наряда C4, а не свободный текст"
    for forbidden in FORBIDDEN_IN_OUTPUT:
        assert forbidden.casefold() not in message.casefold(), (
            f"утечка {forbidden!r} в ответе на {raw[:60]!r}"
        )


def test_classification_categories() -> None:
    assert classify("HTTP 401 Unauthorized: bad key") == "auth"
    assert classify("429 Too Many Requests") == "rate_limit"
    assert classify("provider deepinfra credential is unavailable") == "no_key"
    assert classify("provider deepinfra timed out") == "transient"
    assert classify(ProviderUnavailable("all routed providers unavailable")) == "transient"
    assert classify("ValueError: expecting comma on line 1") == "unknown"


def test_exception_chains_are_classified_by_cause() -> None:
    """BackendUnavailable поверх ProviderUnavailable — всё ещё transient."""
    inner = ProviderUnavailable("all routed providers unavailable")
    outer = RuntimeError("brain backend failed")
    outer.__cause__ = inner
    assert classify(outer) == "transient"


def test_agent_source_has_no_provider_literals_in_user_text() -> None:
    """Структурный тест: литералы с именами провайдеров ушли из ответов."""
    agent_src = (_REPO_ROOT / "core" / "agent.py").read_text(encoding="utf-8")
    orchestrator_src = (_REPO_ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    for forbidden in ("Ошибка DeepInfra/DeepSeek runtime",
                      "Ошибка DeepInfra:", "Ошибка локального runtime:"):
        assert forbidden not in agent_src, forbidden
        assert forbidden not in orchestrator_src, forbidden
    assert "user_message_for" in agent_src, "_handle_model_unavailable идёт через маппер"


def test_handle_input_boundary_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """C4-граница: упавший виток возвращает friendly-ответ, а не исключение."""
    from core.orchestrator import Orchestrator
    from core.state import new_state

    orch = object.__new__(Orchestrator)
    orch._session = SimpleNamespace(push=lambda *a, **kw: None,
                                    to_state=lambda state: None)

    def _raise(*args, **kwargs):
        raise ProviderUnavailable("all routed providers unavailable (deepinfra:ProviderUnavailable)")

    def _fake_new_state(text, *, include_executive=True):
        return new_state(text)

    monkeypatch.setattr(orch, "_handle_input_impl", _raise)
    monkeypatch.setattr(orch, "_new_state", _fake_new_state)

    state = orch.handle_input("привет")

    response = str(state.get("response") or "")
    assert response == MESSAGES["transient"]
    assert "ProviderUnavailable" not in response
    assert "orchestrator" in str(state.get("error") or "").lower() or state.get("error")
    assert state["latency"]["path"] == "error"
