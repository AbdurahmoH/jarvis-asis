"""Sprint 2 — CONVERSATION vs ACTION: разговор не должен видеть инструменты.

Причина фикса: даже для «привет» planner-промпт содержал список инструментов
и просил JSON-план — слабая fast-модель (am/free) галлюцинировала вызовы
list_files/web_fetch на разговорных запросах (подтверждено live smoke).

После фикса:
  * уверенно разговорный запрос -> прямой ответ БЕЗ списка инструментов
    и БЕЗ JSON-плана (mode == "conversation");
  * настоящий action -> прежний planner-путь (tools в промпте, risk gate,
    execute_tool, verify/repair) — без изменений.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.agent import Agent, AgentConfig
from core.model_router import classify_conversation
from core.router.intent_router import resolve_keyword_tool
from tests.conftest import FakeBackend


# --------------------------------------------------------------------------- #
#  Unit: офлайн-классификатор
# --------------------------------------------------------------------------- #

CONVERSATION_CASES = [
    "Привет",
    "Расскажи анекдот",
    "Объясни квантовую механику",
    "Почему небо голубое?",
    "Расскажи короткую историю",
    "Мне скучно, поговори со мной",  # пограничный: не должен звать tool
    "напиши сказку про ёжика",
]

ACTION_CASES = [
    "Какие файлы есть в documents?",
    "Открой файл отчёт.pdf",
    "Найди в интернете информацию о марсах",
    "погода сегодня",                      # доменное существительное без глагола
    "поставь музыку",
    "напомни через 5 минут выпить воду",
    "громкость тише",
]


@pytest.mark.parametrize("goal", CONVERSATION_CASES)
def test_classifier_conversation(goal):
    intent = resolve_keyword_tool(goal, goal)
    is_conv, reason = classify_conversation(goal, intent)
    assert is_conv, f"{goal!r} должен быть conversation (intent={intent})"


@pytest.mark.parametrize("goal", ACTION_CASES)
def test_classifier_action(goal):
    intent = resolve_keyword_tool(goal, goal)
    is_conv, _ = classify_conversation(goal, intent)
    assert not is_conv, f"{goal!r} должен идти в planner (intent={intent})"


def test_classifier_verb_beats_hint():
    """«Расскажи и найди» — действие, несмотря на разговорный маркер."""
    goal = "расскажи про кошек и найди в интернете фото"
    intent = resolve_keyword_tool(goal, goal)
    assert classify_conversation(goal, intent)[0] is False


# --------------------------------------------------------------------------- #
#  Integration: агент реально не показывает инструменты разговору
# --------------------------------------------------------------------------- #

def _make_agent(settings, backend) -> Agent:
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    return agent


@pytest.mark.parametrize("goal", ["Привет", "Объясни квантовую механику", "Мне скучно"])
def test_conversation_direct_answer_without_tools(fake_backend, settings, goal):
    """Разговор: промпт модели НЕ содержит инструментов и JSON-инструкций."""
    agent = _make_agent(settings, fake_backend)
    outcome = agent.execute(goal)

    assert outcome.mode == "conversation"
    assert outcome.text.strip()
    assert not outcome.needs_confirmation

    # Все вызовы модели: ни в одном нет списка инструментов/JSON-плана
    assert fake_backend.calls, "модель должна вызываться"
    for call in fake_backend.calls:
        blob = " ".join(str(m.get("content", "")) for m in call["messages"])
        blob = (call.get("system") or "") + " " + blob
        assert "Доступные инструменты" not in blob, \
            "разговорный промпт не должен содержать инструменты"
        assert "Цель пользователя" not in blob, \
            "разговор не должен идти через planner-промпт"


@pytest.mark.parametrize("goal", [
    "Скопируй файл notes.txt в backup.txt",
    "Открой файл notes.txt",
])
def test_action_uses_planner_with_tools(fake_backend, settings, goal, tmp_path):
    """Action: прежний путь — planner получает инструменты."""
    # Направляем documents_dir в tmp, чтобы file-инструменты работали офлайн.
    settings.paths.documents_dir = str(tmp_path)
    agent = _make_agent(settings, fake_backend)
    fake_backend.set_answer("Сделаю, сэр.")
    outcome = agent.execute(goal)

    # planner-путь должен быть виден в промпте хотя бы одного вызова
    blobs = []
    for call in fake_backend.calls:
        blob = " ".join(str(m.get("content", "")) for m in call["messages"])
        blobs.append((call.get("system") or "") + " " + blob)
    assert any("Доступные инструменты" in b for b in blobs), \
        "action-запрос обязан пройти через planner с инструментами"


def test_web_search_action_goes_planner(fake_backend, settings):
    """«Найди в интернете …» — действие: planner, не прямой разговор."""
    agent = _make_agent(settings, fake_backend)
    fake_backend.set_plan("web_search", {"query": "марсы"}, reason="поиск")
    outcome = agent.execute("Найди в интернете информацию о марсах")
    blobs = [" ".join(str(m.get("content", "")) for m in c["messages"]) for c in fake_backend.calls]
    assert any("Цель пользователя" in b for b in blobs), "planner-промпт должен быть"
    assert outcome.mode in ("tool", "fast_path", "conversation")  # планировщик решил


def test_conversation_streams_via_sink(fake_backend, settings):
    """Прямой разговор стримит кумулятивный текст в sink (Sprint 1 механизм)."""
    agent = _make_agent(settings, fake_backend)
    seen: list[str] = []
    agent.install_stream_sink(lambda visible: seen.append(visible))
    try:
        outcome = agent.execute("Расскажи анекдот")
    finally:
        agent.clear_stream_sink()
    assert outcome.mode == "conversation"
    assert seen, "разговорный ответ должен стримиться"
    # кумулятивность
    assert all(b.startswith(a) for a, b in zip(seen, seen[1:]))
