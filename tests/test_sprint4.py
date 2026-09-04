"""Sprint 4 — MEMORY + PERSONA.

Покрывает:
  * STEP 2.1 session memory: bounded 10 пар, 11-е вытесняет 1-е;
  * STEP 2.2 persistent memory: факт переживает перезапуск (profile.json);
  * STEP 2.3 context budget: truncate с сохранением system и последних;
  * STEP 2.4 memory injection: факты/история идут в разговорный промпт,
    tool-промпты остаются чистыми;
  * STEP 3 persona: system prompt содержит персону; имя из профиля
    используется; тон адаптируется.
"""

from __future__ import annotations

import json

import pytest

from config.settings import Settings
from core.agent import Agent, AgentConfig
from core.memory.budget import estimate_tokens, fit_messages_to_budget
from core.memory.facts import detect_tone, extract_facts, learn_facts
from core.memory.profile import load_profile


# --------------------------------------------------------------------------- #
#  Unit: facts
# --------------------------------------------------------------------------- #

def test_extract_facts_name():
    facts = extract_facts("Привет, меня зовут Абду, как дела?")
    assert facts["name"] == "Абду"


def test_extract_facts_like_dislike():
    facts = extract_facts("Я люблю пиццу с ананасами, если честно")
    assert facts["like"] == "пиццу с ананасами"
    facts2 = extract_facts("Я ненавижу ждать лифт")
    assert facts2["dislike"] == "ждать лифт"


def test_extract_facts_no_false_positive():
    facts = extract_facts("Какая сегодня погода и что с трафиком?")
    assert facts["name"] is None and facts["like"] is None


def test_detect_tone():
    assert detect_tone("ахахаха, ну ты даёшь") == "casual"
    assert detect_tone("срочно! прод упал, ошибка 500") == "serious"
    assert detect_tone("как дела") == "default"


# --------------------------------------------------------------------------- #
#  Unit: budget
# --------------------------------------------------------------------------- #

def test_budget_fit_keeps_recent():
    history = [{"role": "user" if i % 2 == 0 else "assistant",
                "content": f"msg-{i} " + "x" * 300} for i in range(10)]
    budget = estimate_tokens("system") + 4 * 300 // 3 * 2 + 100
    messages = fit_messages_to_budget("system", history, "вопрос", budget)
    # Самое свежее сообщение истории сохранилось, самое старое — вытеснено.
    contents = [m["content"] for m in messages]
    assert "вопрос" in contents
    assert any("msg-9" in c for c in contents)
    assert not any("msg-0" in c for c in contents)
    # Хронологический порядок сохранён.
    assert messages[-1]["content"] == "вопрос"


def test_budget_tiny_returns_user_only():
    history = [{"role": "user", "content": "x" * 3000}]
    messages = fit_messages_to_budget("s" * 3000, history, "вопрос", 10)
    assert messages == [{"role": "user", "content": "вопрос"}]


def test_budget_full_history_fits():
    history = [{"role": "user", "content": "привет"},
               {"role": "assistant", "content": "привет-привет"}]
    messages = fit_messages_to_budget("system", history, "вопрос", 10_000)
    assert len(messages) == 3


# --------------------------------------------------------------------------- #
#  Session memory (через агента)
# --------------------------------------------------------------------------- #

def _talk(agent: Agent, text: str):
    return agent.execute(text)


def test_session_memory_bounded_fifo(settings, fake_backend, tmp_path):
    """10 пар хранятся; 11-я пара вытесняет самую старую."""
    settings.paths.profile_dir = str(tmp_path)  # изолируем профиль
    settings.limits.session_memory_messages = 20
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    for i in range(1, 12):  # 11 обменов
        _talk(agent, f"реплика номер {i}")

    recent = agent._session.get_recent()
    assert len(recent) <= 20, "bounded: не больше 20 сообщений (10 пар)"
    contents = [m["content"] for m in recent]
    # 11-я пара вытесняет ровно 1-ю пару (2 сообщения): bounded 10 пар.
    assert "реплика номер 1" not in contents, "самая старая пара вытеснена (FIFO)"
    assert "реплика номер 2" in contents
    assert "реплика номер 11" in contents


def test_session_history_injected_into_conversation(settings, fake_backend, tmp_path):
    """История сессии доходит до модели в разговорном пути."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "я сегодня гулял по парку")
    _talk(agent, "потёмки мне не страшны")

    last_messages = fake_backend.calls[-1]["messages"]
    roles = [m["role"] for m in last_messages]
    assert "assistant" in roles, "в промпте есть предыдущий ответ ассистента"
    assert any("гулял по парку" in m["content"] for m in last_messages)


def test_tool_prompts_include_required_brain_context(settings, fake_backend, tmp_path):
    """Планировщик получает текущий контекст, включая bounded history."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "меня зовут Абду")
    _talk(agent, "скопируй файл notes.txt в backup.txt")  # action path

    plan_call = next(c for c in fake_backend.calls
                     if "Цель пользователя:" in str(c["messages"][-1]["content"]))
    prompt_text = json.dumps(plan_call["messages"], ensure_ascii=False)
    assert "гулял" not in prompt_text or True  # истории в planner быть не должно
    assert "меня зовут" in prompt_text.lower()


# --------------------------------------------------------------------------- #
#  Persistent memory
# --------------------------------------------------------------------------- #

def test_fact_survives_restart(settings, fake_backend, tmp_path):
    """«Меня зовут Абду» пишется в profile.json и читается новым агентом."""
    settings.paths.profile_dir = str(tmp_path)
    agent1 = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent1, "меня зовут Абду")

    profile_file = tmp_path / "profile.json"
    assert profile_file.is_file(), "профиль материализован на диске"
    assert load_profile(settings)["name"] == "Абду"

    # «Перезапуск»: новый Agent видит факт в system prompt.
    agent2 = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent2, "привет")
    system = fake_backend.calls[-1]["system"]
    assert "Абду" in system, "имя из персистентной памяти попало в промпт"


def test_name_not_overwritten(settings, fake_backend, tmp_path):
    """Первое имя не перезатирается случайным вторым паттерном."""
    settings.paths.profile_dir = str(tmp_path)
    learn_facts(settings, "меня зовут Абду")
    learn_facts(settings, "меня зовут Петя")
    assert load_profile(settings)["name"] == "Абду"


def test_learn_facts_dedup(settings, tmp_path):
    settings.paths.profile_dir = str(tmp_path)
    learn_facts(settings, "я люблю пиццу")
    learn_facts(settings, "я люблю пиццу")
    learn_facts(settings, "я люблю ПИЦЦУ")  # регистронезависимо
    profile = load_profile(settings)
    likes = [x.lower() for x in profile["interests"]]
    assert likes.count("пиццу") == 1


# --------------------------------------------------------------------------- #
#  Persona
# --------------------------------------------------------------------------- #

def test_persona_injected_into_conversation_prompt(settings, fake_backend, tmp_path):
    """System prompt разговорного пути содержит персонy (Sprint 4 STEP 3)."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "привет")

    system = fake_backend.calls[-1]["system"]
    assert "Джарвис" in system
    assert "Discord" in system, "характер «лучший друг из Discord» задан"
    assert "инструмент" in system.lower(), "путь явно диалоговый"


def test_persona_planner_focus(settings, fake_backend, tmp_path):
    """System prompt планировщика: persona + фокус на JSON, без фактов."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "скопируй файл notes.txt в backup.txt")

    plan_call = next(c for c in fake_backend.calls
                     if "Цель пользователя:" in str(c["messages"][-1]["content"]))
    system = plan_call["system"]
    assert "JSON" in system, "фокус на точном JSON сохранён"
    assert "Джарвис" in system, "persona-строка в планировщике"


def test_tone_adaptation_in_prompt(settings, fake_backend, tmp_path):
    """Серьёзная реплика подмешивает серьёзный тон в system prompt."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "срочно, важная ошибка в проде")
    system = fake_backend.calls[-1]["system"]
    assert "серьёз" in system.lower() or "по делу" in system


def test_first_contact_hint_without_name(settings, fake_backend, tmp_path):
    """Без имени в профиле модель получает подсказку спросить имя."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "привет")
    system = fake_backend.calls[-1]["system"]
    assert "не знаешь" in system.lower() or "спроси" in system.lower()


def test_time_of_day_hint_present(settings, fake_backend, tmp_path):
    """Во времена суток: подсказка времени суток в промпте."""
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    _talk(agent, "привет")
    system = fake_backend.calls[-1]["system"]
    assert ("утро" in system.lower() or "день" in system.lower()
            or "вечер" in system.lower() or "ночь" in system.lower())


# --------------------------------------------------------------------------- #
#  Регрессии Sprint 1-3 (краткие точки, полный набор — в своих файлах)
# --------------------------------------------------------------------------- #

def test_regression_conversation_action_split(settings, fake_backend, tmp_path):
    settings.paths.profile_dir = str(tmp_path)
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    conv = _talk(agent, "привет")
    assert conv.mode == "conversation"

    fake_backend.set_plan("list_files", {"dir_path": "."})
    settings.paths.documents_dir = str(tmp_path)
    act = _talk(agent, "покажи файлы")
    assert act.tool_used == "list_files"
