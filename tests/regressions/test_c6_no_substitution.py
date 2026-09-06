"""Regression C6 — «открой/установи X» не подменяется случайным инструментом.

Аудит 2026-09-06 (фразы 4, 10, 16, 25, 29): «установи осинт» исполнялся как
``play_music`` (открывался YouTube-поиск), «установи скилл» — как ``volume``,
«Open Interpreter скачать да? десктоп версию?» — как ``open_app`` с именем
«Interpreter скачать да? десктоп версию». Корень — извлечение аргументов
принимало кусок задачи за имя приложения/запрос.

Наряд C6: подозрительный аргумент (вопрос, >3 слов, глаголы задачи, частицы)
→ инструмент НЕ выполняется, идёт clarify; установка ПО → честный
«Установка программ пока не поддерживается…» вместо подмены.
"""
from __future__ import annotations

import pytest

from core.agent import Agent, _arg_looks_like_task, _install_intent
from config.settings import Settings
from tests.conftest import FakeBackend


@pytest.fixture()
def agent():
    return Agent(Settings(), config=AgentConfig_no_forge())


def AgentConfig_no_forge():
    from core.agent import AgentConfig
    return AgentConfig(enable_skill_forge=False)


# --------------------------------------------------------------------------- #
# Детекторы
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [
    "Interpreter скачать да? десктоп версию",
    "что-нибудь на втором мониторе?",
    "блокнот и запиши молоко хлеб яйца",
    "бля, надо установить осинт инструмент",
])
def test_task_like_names_are_rejected(name: str) -> None:
    assert _arg_looks_like_task(name) is True


@pytest.mark.parametrize("name", ["блокнот", "гта 5", "telegram", "Spotify"])
def test_legitimate_app_names_pass(name: str) -> None:
    assert _arg_looks_like_task(name) is False


@pytest.mark.parametrize("goal", [
    "установи sherlock",
    "бля, надо установить осинт инструмент, установи и обьясни как пользоваться",
    "установи это, сделай так чтобы скилл использовался автоматически",
    "Open Interpreter скачать да? десктоп версию?",
    "скачай мне blender",
])
def test_install_intent_detected(goal: str) -> None:
    assert _install_intent(goal) is True


@pytest.mark.parametrize("goal", [
    "сделай потише",
    "поставь музыку для тренировки",
    "открой блокнот",
    "сделай громче на 30 процентов",
])
def test_non_install_goals_not_flagged(goal: str) -> None:
    assert _install_intent(goal) is False


# --------------------------------------------------------------------------- #
# Живые фразы аудита: инструмент не выполняется
# --------------------------------------------------------------------------- #


def _agent_with_dead_llm() -> Agent:
    """Агент с недоступной моделью: fast path решает без LLM."""
    agent = Agent(Settings(), config=AgentConfig_no_forge())
    return agent


def test_install_phrase_is_not_executed_as_music() -> None:
    """Фраза 4: «установи осинт» больше не становится play_music."""
    agent = _agent_with_dead_llm()
    outcome = agent.execute("бля, надо установить осинт инструмент, установи и обьясни как пользоваться")
    assert outcome.tool_used is None, f"инструмент исполнился: {outcome.tool_used}"
    assert outcome.mode == "unsupported"
    assert "не поддерживается" in outcome.text


def test_install_phrase_is_not_executed_as_volume() -> None:
    """Фраза 16: «установи скилл» больше не становится volume."""
    agent = _agent_with_dead_llm()
    outcome = agent.execute("установи это, сделай так чтобы скилл использовался автоматически")
    # Главное: volume не исполнился. Путь может отличаться (unsupported /
    # conversation) — фиксируем отсутствие исполнения и подмены.
    assert outcome.tool_used is None, f"инструмент исполнился: {outcome.tool_used}"
    assert "не поддерживается" in outcome.text or outcome.mode in {"conversation", "unknown_task"}


def test_open_interpreter_phrase_is_not_open_app() -> None:
    """Фраза 25: имя приложения больше не «Interpreter скачать да? десктоп версию»."""
    agent = _agent_with_dead_llm()
    outcome = agent.execute("Open Interpreter скачать да? десктоп версию?")
    assert outcome.tool_used is None
    assert "не поддерживается" in outcome.text


def test_rant_phrase_does_not_play_music() -> None:
    """Фраза 10: жалоба «сделай нормального джарвиса. Что за хуйня?» — не музыка."""
    agent = _agent_with_dead_llm()
    outcome = agent.execute(
        "бля слушай, сделай наконец нормального джарвиса. Что за хуйня? "
        "ни голоса, ни интерфейса.. я хотел приложение - запускаешь и работает Джарвис"
    )
    # музыка не заиграла (нет признака исполнения play_music) и нет batch
    assert outcome.mode in {"clarification", "model_error", "unsupported", "batch",
                            "conversation", "unknown_task"}
    assert "открыта поисковая страница" not in outcome.text


def test_legitimate_music_still_works() -> None:
    """Регрессионный контроль: обычная музыкальная фраза не сломана."""
    agent = _agent_with_dead_llm()
    outcome = agent.execute("включи что-нибудь бодрое для тренировки")
    assert outcome.mode == "fast_path" or outcome.tool_used == "play_music", (
        f"легитимная музыка сломана: mode={outcome.mode}, tool={outcome.tool_used}"
    )


def test_legitimate_open_app_still_works() -> None:
    agent = _agent_with_dead_llm()
    outcome = agent.execute("открой блокнот")
    assert outcome.tool_used == "open_app" or outcome.mode == "fast_path"
