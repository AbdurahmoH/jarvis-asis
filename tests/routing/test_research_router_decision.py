"""Research-режим определяется роутером (decision.is_research) — R1/аудит 1a.

Ключевое слово-классификатор is_research_goal (core/research.py) удалён как
второй классификатор после decision. Research-подкласс миссий задаётся
отдельным классом якорей ANCHORS_RESEARCH (не из eval set) и читается
агентом из решения единого роутера.
"""
from __future__ import annotations

from core.routing.semantic_router import RoutingContext, route

OFFLINE_STRICT = RoutingContext(llm_available=False, allow_clarify=False)


def test_research_mission_detected_by_router():
    decision = route("изучи проект FastAPI и сравни с Flask", OFFLINE_STRICT)
    assert decision.kind == "mission"
    assert decision.is_research is True


def test_documentation_study_is_research():
    decision = route(
        "изучи документацию fastapi и сделай краткий конспект лучших практик",
        OFFLINE_STRICT,
    )
    assert decision.kind == "mission"
    assert decision.is_research is True


def test_regular_mission_is_not_research():
    decision = route("подготовь презентацию о внедрении ИИ на 10 слайдов", OFFLINE_STRICT)
    assert decision.kind == "mission"
    assert decision.is_research is False


def test_essay_mission_is_not_research():
    decision = route(
        "напиши развёрнутое эссе о том, как паровые машины изменили "
        "промышленность, транспорт и жизнь городов в девятнадцатом веке",
        OFFLINE_STRICT,
    )
    assert decision.kind == "mission"
    assert decision.is_research is False


def test_simple_command_is_not_research():
    decision = route("открой блокнот", OFFLINE_STRICT)
    assert decision.is_research is False


def test_is_research_survives_dict_roundtrip():
    decision = route("изучи тему и собери материал из разных источников", OFFLINE_STRICT)
    assert decision.is_research is True
    restored = type(decision).from_dict(decision.to_dict())
    assert restored.is_research is True
    assert restored.kind == decision.kind


def test_agent_routes_research_via_router_decision(settings):
    """_handle_research вызывается только при decision.is_research (гейт в agent.py)."""
    import inspect

    from core.agent import Agent

    source = inspect.getsource(Agent._execute_core)
    assert "is_research_goal(" not in source, "keyword-классификатор is_research_goal не должен вызываться"
    assert "decision.is_research" in source, "research-гейт обязан читать решение роутера"
