"""Интеграционные тесты P0-спринта (TEST1-7) на реальном Agent'е.

Стратегия: подменяем ТОЛЬКО нижний слой LLM (FakeBackend), но прогоняем
всю остальную реальную цепочку Джарвиса без сети/моделей:
  intent -> risk -> MODEL SELECTION (router) -> tool retrieval
  -> plan -> execute -> verify -> repair -> confirmation -> memory.

Все тесты работают офлайн и детерминированно.
"""

from __future__ import annotations

import threading

import pytest

from core.agent import Agent, AgentConfig
from core.llm import Tier
from core.memory.knowledge_graph import GraphMemoryStore
from core.router.intent_router import resolve_keyword_tool
from core.task_runtime import MissionStatus


def _make_agent(settings, fake_backend) -> Agent:
    """Agent с фейковым бэкендом и детерминированным поведением.

    Выключаем Skill Forge (не нужен для тестов) и интернет-инструменты
    в retrieval, чтобы не лезть в сеть.
    """
    cfg = AgentConfig(enable_skill_forge=False)
    agent = Agent(settings, config=cfg)
    # GraphMemoryStore уже создан в __init__; очистим его для чистоты теста.
    if agent._graph is not None:
        try:
            agent._graph._conn.execute("DELETE FROM nodes")
            agent._graph._conn.commit()
        except Exception:
            pass
    return agent


# --------------------------------------------------------------------------- #
#  TEST 1 — SIMPLE: "привет" -> быстрый ответ, БЕЗ тяжёлого mission loop
# --------------------------------------------------------------------------- #
def test_p0_simple_greeting_sync(settings, fake_backend):
    agent = _make_agent(settings, fake_backend)
    fake_backend.set_answer("Здравствуйте, сэр. Чем могу помочь?")
    outcome = agent.execute("привет")
    assert outcome is not None
    assert outcome.needs_confirmation is False
    assert outcome.mode == "conversation"
    assert "здравствуйте" in outcome.text.lower() or outcome.text.strip()


# --------------------------------------------------------------------------- #
#  TEST 3 — TOOL: filesystem-инструмент проходит end-to-end через execute+verify
# --------------------------------------------------------------------------- #
def test_p0_tool_list_files_end_to_end(settings, fake_backend, tmp_path):
    agent = _make_agent(settings, fake_backend)
    # list_files по умолчанию смотрит в documents_dir; направим через tmp.
    settings.paths.documents_dir = str(tmp_path)
    (tmp_path / "hello.txt").write_text("привет", encoding="utf-8")
    # Схема ListFilesTool: аргумент dir_path (не path).
    fake_backend.set_plan("list_files", {"dir_path": "."})
    outcome = agent.execute("покажи файлы")
    assert outcome.mode in ("tool", "fast_path")
    assert outcome.tool_used == "list_files"
    assert outcome.verified is True  # verifier нашёл листинг


# --------------------------------------------------------------------------- #
#  TEST 4 — FAILURE: несуществующий файл -> graceful, НЕ "готово"
# --------------------------------------------------------------------------- #
def test_p0_failure_nonexistent_file(settings, fake_backend, tmp_path):
    agent = _make_agent(settings, fake_backend)
    settings.paths.documents_dir = str(tmp_path)
    fake_backend.set_plan("read_file", {"path": "does_not_exist_12345.txt"})
    outcome = agent.execute("прочитай файл does_not_exist_12345.txt")
    # Честно: verification НЕ прошла, мы не говорим "готово".
    assert outcome.verified is False
    assert "не удалось" in outcome.text.lower() or "не" in outcome.text.lower()


# --------------------------------------------------------------------------- #
#  TEST 5 — HIGH RISK: confirmation -> answer -> execution
# --------------------------------------------------------------------------- #
def test_p0_high_risk_confirmation_loop(settings, fake_backend, tmp_path, monkeypatch):
    agent = _make_agent(settings, fake_backend)
    settings.paths.documents_dir = str(tmp_path)
    # Относительные пути в tool-плане резолвятся относительно CWD;
    # направим CWD туда же, куда пишет documents_dir, чтобы verifier
    # нашёл созданный файл.
    monkeypatch.chdir(tmp_path)
    # HIGH-risk через текст цели: "удали файл ..." + инструмент write_file.
    fake_backend.set_plan("write_file", {"path": "danger.txt", "content": "x"})
    outcome = agent.execute("удали файл danger.txt и создай новый")
    assert outcome.needs_confirmation is True, outcome.text
    assert outcome.confirmation_id, "confirmation_id должен быть выдан"
    cid = outcome.confirmation_id

    # Отклоняем -> отмена.
    rejected = agent.answer_confirmation(cid, approved=False)
    assert rejected is not None
    assert rejected.mode == "confirmation_rejected"

    # Подтверждаем новый запрос -> выполнение инструмента.
    fake_backend.set_plan("write_file", {"path": "danger.txt", "content": "x"})
    out2 = agent.execute("удали файл danger.txt и создай новый")
    assert out2.needs_confirmation is True
    cid2 = out2.confirmation_id
    approved = agent.answer_confirmation(cid2, approved=True)
    assert approved is not None
    assert approved.mode == "tool"
    assert approved.tool_used == "write_file"
    assert (tmp_path / "danger.txt").exists()


# --------------------------------------------------------------------------- #
#  TEST 6 — MODEL ROUTING: решение ModelRouter реально доходит до get_llm_backend
# --------------------------------------------------------------------------- #
def test_p0_model_routing_propagation(settings, fake_backend, tmp_path):
    agent = _make_agent(settings, fake_backend)
    settings.paths.documents_dir = str(tmp_path)
    (tmp_path / "report.txt").write_text("hello", encoding="utf-8")
    fake_backend.set_plan("read_file", {"path": "report.txt"})
    agent.execute("прочитай файл report.txt")
    # FakeBackend._fake_get записывает каждый запрошенный тир.
    tiers = fake_backend.requested_tiers
    assert len(tiers) >= 1, "get_llm_backend вообще не вызывался"
    # Роутер должен выбрать тир, а не молча FAST; проверяем, что
    # запрашиваемый тир совпадает с тем, что вернул ModelRouter.
    # Sprint 3 TIER 2: для JSON-плана маршрут проходит route_for_planning
    # (планировщик поднимается с FAST до первого внешнего тира).
    from core.model_router import ModelRouter
    router = ModelRouter(settings)
    decision = router.route_for_planning(router.route("прочитай файл report.txt"))
    assert decision.tier in tiers, (
        f"ModelRouter выбрал {decision.tier}, но get_llm_backend вызвался с {tiers}"
    )


# --------------------------------------------------------------------------- #
#  TEST 7 — MEMORY: store (успешный tool) -> retrieve -> use
# --------------------------------------------------------------------------- #
def test_p0_memory_store_retrieve_use(settings, fake_backend, tmp_path):
    agent = _make_agent(settings, fake_backend)
    settings.paths.documents_dir = str(tmp_path)
    (tmp_path / "note.txt").write_text("data", encoding="utf-8")
    # Схема ListFilesTool: dir_path (не path).
    agent.execute("покажи файлы")
    # После успешного выполнения факт должен попасть в граф-память.
    ctx = agent._retrieve_context("list_files")
    assert ctx.strip(), "Память должна вернуть сохранённый факт"
    # Сохранённый факт содержит цель и результат листинга.
    assert "покажи файлы" in ctx or "note.txt" in ctx


# --------------------------------------------------------------------------- #
#  TEST 2 — COMPLEX: тяжёлая задача -> фон (submit_goal даёт ACK и миссию)
# --------------------------------------------------------------------------- #
def test_p0_complex_background_mission(settings, fake_backend, tmp_path):
    """Проверяем детерминированные примитивы решения о фоне (Orchestrator-уровень).

    Полный Orchestrator тянет voice/proactor/TTS (тяжело и офлайн-небезопасно
    в тестах), поэтому проверяем логику напрямую: сложная формулировка
    должна уходить в фон, а «привет» — нет.
    """
    from core.model_router import estimate_complexity
    from core.research import is_research_goal

    goal_complex = "проанализируй содержимое папки и составь подробный отчёт о структуре"
    goal_simple = "привет"

    # Сложная/исследовательская цель -> фон.
    assert is_research_goal(goal_complex) or estimate_complexity(goal_complex).score >= 0.35
    # Простая -> синхронно.
    assert estimate_complexity(goal_simple).score < 0.35
    assert is_research_goal(goal_simple) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
