"""Regression S1 — оценка риска монотонна: приписанное слово не снимает гейт.

Аудит 2026-09-05 нашёл в ``core/routing/semantic_router.assess_risk`` три
дефекта, которые превращали Risk Gate в источник ЛОЖНЫХ РАЗРЕШЕНИЙ:

1. Секции 1–6 присваивали уровень напрямую (``level = "high"``), а не через
   ``max_level``. Поэтому более позднее правило ПОНИЖАЛО вердикт более раннего:
   «отключи брандмауэр» получал critical в правиле безопасности и тут же
   опускался до high правилом секретов. Инверсия проверялась на живой системе:
   «удали файл hosts» (high) оценивался НИЖЕ, чем «покажи файл hosts» (critical).
2. Цепочка ``elif`` в секции 6 гасила ветки с более высоким уровнем: раннее
   совпадение массовости (high) пропускало ветку форматирования (critical).
3. Регулярки шли без границ ``\\b``: ``ключ\\w*`` совпадал внутри «в-ключи»,
   «от-ключи», «под-ключи», из-за чего «включи музыку» становилась HIGH и
   требовала подтверждения, а «отключи брандмауэр» — понижалась (см. п.1).

Почему это дыра, а не косметика: ``core/authority.py`` сравнивает
``request.risk`` с ``grant.risk_ceiling``, и совпавший грант возвращает
``needs_confirmation=False``. Понижение critical→high означает, что грант с
ceiling=high молча авторизует критическое действие.

Тесты ниже закрепляют: (1) свойство монотонности на всём eval-наборе,
(2) паспорт возможности как пол уровня, (3) структурный запрет на присваивание
уровня мимо ``max_level``, (4) конкретные фразы из ТЗ, (5) ceiling=high не
покрывает critical, (6) fast path не исполняет инструмент при needs_confirmation.
"""
from __future__ import annotations

import ast
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.capabilities import CAPABILITIES, RiskLevel
from core.routing.semantic_router import assess_risk as router_assess_risk
from core.routing.semantic_router import max_level
from core.safety import assess_risk as safety_assess_risk

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTER_SRC = _REPO_ROOT / "core" / "routing" / "semantic_router.py"
_EVAL_SET = _REPO_ROOT / "tests" / "routing" / "routing_eval_set.json"

_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

#: Суффиксы из ТЗ S1(e). «отключи» — главный: именно он ловил `ключ\w*`.
_SUFFIXES = ("сегодня", "пожалуйста", "отключи", "сейчас")


def _phrases(limit: int = 200) -> list[str]:
    items = json.loads(_EVAL_SET.read_text(encoding="utf-8"))
    return [str(item["text"]) for item in items[:limit]]


# ---------------------------------------------------------------------------
# 1. Свойство: дописанное слово НИКОГДА не понижает уровень
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_appending_words_never_lowers_risk(suffix: str) -> None:
    """200 фраз eval-набора × суффикс: уровень не убывает, гейт не снимается."""
    regressions: list[str] = []
    for phrase in _phrases():
        base_level, base_conf = router_assess_risk(None, None, phrase)
        long_level, long_conf = router_assess_risk(None, None, f"{phrase} {suffix}")
        if _ORDER[long_level] < _ORDER[base_level]:
            regressions.append(
                f"{phrase!r} + {suffix!r}: {base_level} -> {long_level} (понижение)"
            )
        elif base_conf and not long_conf:
            regressions.append(
                f"{phrase!r} + {suffix!r}: подтверждение снято приписанным словом"
            )
    assert not regressions, "Оценка риска не монотонна:\n" + "\n".join(regressions)


def test_appending_words_never_lowers_risk_through_safety_gate() -> None:
    """То же свойство у боевого гейта core.safety.assess_risk (он берёт максимум)."""
    regressions: list[str] = []
    for phrase in _phrases(60):
        base = safety_assess_risk(phrase)
        for suffix in _SUFFIXES:
            longer = safety_assess_risk(f"{phrase} {suffix}")
            if _ORDER[longer.level.value] < _ORDER[base.level.value]:
                regressions.append(
                    f"{phrase!r} + {suffix!r}: {base.level.value} -> {longer.level.value}"
                )
    assert not regressions, "core.safety.assess_risk не монотонна:\n" + "\n".join(regressions)


# ---------------------------------------------------------------------------
# 2. Паспорт возможности — пол уровня (S1a): ни одно правило не опускает ниже
# ---------------------------------------------------------------------------

def test_capability_passport_is_the_floor_for_every_tool() -> None:
    """Для каждой возможности уровень ≥ паспортного (без per-action аргументов)."""
    too_low: list[str] = []
    for cap in CAPABILITIES.all():
        level, _ = router_assess_risk(cap.name, {}, "")
        if _ORDER[level] < _ORDER[cap.risk_level.value]:
            too_low.append(f"{cap.name}: паспорт {cap.risk_level.value}, оценка {level}")
    assert not too_low, "Уровень ниже паспорта возможности:\n" + "\n".join(too_low)


@pytest.mark.parametrize("tool", ["screen_capture", "computer_screenshot"])
def test_screen_capture_is_gated_by_passport_not_by_override(tool: str) -> None:
    """S1d: снимок экрана больше не выключает паспортный риск (было level=low)."""
    cap = CAPABILITIES.get(tool)
    assert cap is not None, f"{tool} исчез из реестра возможностей"
    level, needs_confirmation = router_assess_risk(tool, {}, "покажи что на экране")
    assert level == cap.risk_level.value, (
        f"{tool}: ожидался паспортный уровень {cap.risk_level.value}, получен {level}"
    )
    assert needs_confirmation is (cap.risk_level.value in ("high", "critical"))
    src = _ROUTER_SRC.read_text(encoding="utf-8")
    assert "_use_passport_risk" not in src, (
        "Флаг отключения паспортного риска вернулся в semantic_router"
    )


def test_safety_gate_does_not_zero_screenshot_passport() -> None:
    """То же исключение стояло вторым экземпляром в core/safety.py — снято."""
    src = (_REPO_ROOT / "core" / "safety.py").read_text(encoding="utf-8")
    assert 'tool == "computer_screenshot"' not in src, (
        "core/safety.py снова обнуляет паспортный риск снимка экрана"
    )
    assessment = safety_assess_risk("покажи что на экране", "computer_screenshot", {})
    assert assessment.level is RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# 3. Структурный запрет: уровень задаётся один раз, дальше только max_level
# ---------------------------------------------------------------------------

def test_assess_risk_assigns_level_only_through_max_level() -> None:
    """AST-проверка: сырое присваивание level разрешено только для базы.

    Это защита от повторения дефекта: любое ``level = "high"`` в теле функции
    может ПОНИЗИТЬ уже поднятый уровень, поэтому запрещено структурно, а не
    только тестом на конкретную фразу. Разрешены ровно два присваивания:

    * ``level = "low"`` — инициализация; ``low`` — дно решётки уровней, ниже
      опускать нечего;
    * ``level = passport_level`` — база из паспорта возможности (S1a).

    Всё остальное обязано идти через ``max_level``. Дополнительно проверяется
    порядок: база из паспорта стоит ДО всех эскалаций, иначе она их затирает.
    """
    tree = ast.parse(_ROUTER_SRC.read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "assess_risk"
    )
    bare: list[str] = []
    init_lines: list[int] = []
    base_lines: list[int] = []
    escalation_lines: list[int] = []
    for node in ast.walk(func):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "level":
            bare.append(f"строка {node.lineno}: AugAssign level")
            continue
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "level" for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id == "max_level":
            escalation_lines.append(node.lineno)
            continue
        if isinstance(value, ast.Constant) and value.value == "low":
            init_lines.append(node.lineno)
            continue
        if isinstance(value, ast.Name) and value.id == "passport_level":
            base_lines.append(node.lineno)
            continue
        bare.append(f"строка {node.lineno}: level = {ast.unparse(value)}")
    assert not bare, (
        "Присваивание уровня мимо max_level (может понизить риск):\n" + "\n".join(bare)
    )
    assert len(init_lines) == 1, f"level инициализируется {len(init_lines)} раз, ожидался 1"
    assert len(base_lines) == 1, (
        f"База уровня должна задаваться ровно один раз из паспорта, найдено {len(base_lines)}"
    )
    assert escalation_lines, "эскалации через max_level исчезли — оценка перестала работать"
    assert base_lines[0] < min(escalation_lines), (
        f"паспортная база (строка {base_lines[0]}) стоит после эскалации "
        f"(строка {min(escalation_lines)}) и затирает её"
    )
    assert init_lines[0] < base_lines[0], (
        "инициализация level должна идти до паспортной базы"
    )


def test_max_level_never_returns_lower_than_either_argument() -> None:
    """max_level — действительно максимум (на нём держится вся монотонность)."""
    for a in _ORDER:
        for b in _ORDER:
            result = max_level(a, b)
            assert _ORDER[result] >= max(_ORDER[a], _ORDER[b]), f"max_level({a},{b})={result}"


# ---------------------------------------------------------------------------
# 4. Конкретные фразы из ТЗ S1(e)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase",
    ["включи музыку", "отключи звук", "подключи наушники", "выключи звук", "включи свет"],
)
def test_media_commands_are_not_secret_handling(phrase: str) -> None:
    """`ключ\\w*` больше не совпадает внутри «в-ключи»: бытовая команда = low."""
    level, needs_confirmation = router_assess_risk(None, None, phrase)
    assert level == "low", f"{phrase!r} оценена как {level}"
    assert needs_confirmation is False
    assessment = safety_assess_risk(phrase)
    assert assessment.needs_confirmation is False, (
        f"{phrase!r} требует подтверждения: {assessment.reasons}"
    )


@pytest.mark.parametrize(
    "phrase",
    ["отключи брандмауэр", "выключи defender", "отключи антивирус", "отключи защиту"],
)
def test_security_weakening_stays_critical(phrase: str) -> None:
    """Правило секретов больше не понижает вердикт правила безопасности."""
    level, needs_confirmation = router_assess_risk(None, None, phrase)
    assert level == "critical", f"{phrase!r} оценена как {level}, ожидался critical"
    assert needs_confirmation is True


@pytest.mark.parametrize(
    "phrase",
    ["удали всё из System32", "удали все файлы в C:\\Windows\\System32", "форматируй диск и удали всё"],
)
def test_mass_destruction_of_system_paths_is_critical(phrase: str) -> None:
    level, needs_confirmation = router_assess_risk(None, None, phrase)
    assert level == "critical", f"{phrase!r} оценена как {level}"
    assert needs_confirmation is True


def test_deleting_a_system_file_is_not_ranked_below_reading_it() -> None:
    """Найденная инверсия: удаление hosts оценивалось НИЖЕ его чтения."""
    delete_level, _ = router_assess_risk(None, None, "удали файл hosts")
    read_level, _ = router_assess_risk(None, None, "покажи файл hosts")
    assert _ORDER[delete_level] >= _ORDER[read_level], (
        f"удаление={delete_level} ниже чтения={read_level}"
    )
    assert delete_level == "critical"


# ---------------------------------------------------------------------------
# 5. S1(f): грант с ceiling=high не покрывает critical
# ---------------------------------------------------------------------------

def test_high_ceiling_grant_does_not_cover_critical_action(tmp_path) -> None:
    """core/authority.py: risk_ceiling=high отклоняет critical-запрос.

    Сценарий из аудита: «отключи брандмауэр» оценивается critical, поэтому
    делегированный грант с потолком high обязан требовать подтверждение, а не
    молча авторизовать действие.
    """
    from core.authority import (
        AuthorityProposal, AuthorityRequest, AuthorityStore, ProvenanceKind,
    )

    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    store = AuthorityStore(tmp_path, clock=lambda: now)
    proposal = AuthorityProposal(
        principal="local-user", delegate="jarvis",
        subjects=["localhost"], resources=["security-settings"],
        allowed_actions=["change_security"], capability_families=["security"],
        allowed_effects=["security"], denied_actions=[],
        purposes=["harden the machine"], risk_ceiling=RiskLevel.HIGH,
        valid_from=now, expires_at=now + timedelta(hours=1),
        mission_id=None, constraints={},
    )
    store.issue(
        proposal, source_kind=ProvenanceKind.USER_INSTRUCTION, source_role="user",
        source_text="разрешаю настройку безопасности", source_id="user-message-1",
    )

    goal = "отключи брандмауэр"
    level, needs_confirmation = router_assess_risk(None, None, goal)
    assert (level, needs_confirmation) == ("critical", True)

    decision = store.check(AuthorityRequest(
        subject="localhost", resource="security-settings", action="change_security",
        capability_family="security", effect="security", purpose="harden the machine",
        risk=RiskLevel(level),
    ))

    assert decision.allowed is False, "грант с ceiling=high покрыл critical-действие"
    assert decision.requires_confirmation is True
    assert decision.reason == "risk ceiling exceeded"


# ---------------------------------------------------------------------------
# 6. S1(g): fast path проходит тот же гейт риска
# ---------------------------------------------------------------------------

def _decision(tool: str):
    from core.routing.semantic_router import RoutingDecision

    return RoutingDecision(
        kind="action", tool=tool, confidence=0.95, tier="semantic",
        risk="low", needs_confirmation=False,
    )


def _agent(settings):
    from core.agent import Agent, AgentConfig

    council = MagicMock()
    council.route.return_value = MagicMock(tier=MagicMock(value="fast"), reason="test")
    return Agent(settings, council=council, config=AgentConfig(enable_skill_forge=False))


def test_fast_path_refuses_when_risk_needs_confirmation(settings) -> None:
    """Переданная оценка с needs_confirmation=True останавливает fast path."""
    from core.safety import RiskAssessment

    agent = _agent(settings)
    risk = RiskAssessment(level=RiskLevel.CRITICAL, reasons=["тест"], tool=None)
    with patch("core.agent.execute_tool") as executor:
        outcome = agent._try_fast_path(
            "удали всё из System32", _decision("search_files"), None,
            threading.Event(), risk,
        )
    assert outcome is None, "fast path выполнился при needs_confirmation=True"
    assert not executor.called, "инструмент исполнен в обход подтверждения"


def test_fast_path_rechecks_risk_with_extracted_arguments(settings) -> None:
    """Второй гейт: даже с заниженной входной оценкой fast path пересчитывает риск.

    Входной ``risk`` подменён на low, но ``assess_risk(goal, tool, args)`` с
    извлечёнными аргументами даёт high — инструмент исполняться не должен.
    Контрольный прогон в конце (тот же вызов, но пересчёт возвращает low)
    доказывает, что остановил именно гейт риска, а не другой фильтр fast path.
    """
    from core.safety import RiskAssessment

    goal = "найди файлы паролей"
    agent = _agent(settings)
    recomputed = safety_assess_risk(goal, "search_files", {"query": "паролей", "dir_path": ""})
    assert recomputed.needs_confirmation is True, (
        "фикстура сломалась: цель перестала быть высокорисковой"
    )

    low = RiskAssessment(level=RiskLevel.LOW, reasons=[], tool=None)
    with patch("core.agent.execute_tool") as executor:
        outcome = agent._try_fast_path(goal, _decision("search_files"), None,
                                       threading.Event(), low)
    assert outcome is None, "fast path исполнил high-risk действие по заниженной оценке"
    assert not executor.called

    with patch("core.agent.Agent._execute_verified") as execute_verified, \
            patch("core.agent.assess_risk", return_value=low):
        agent._try_fast_path(goal, _decision("search_files"), None,
                             threading.Event(), low)
    assert execute_verified.called, (
        "контроль не сработал: fast path останавливает не гейт риска, а другой фильтр — "
        "тест выше проходил бы и без проверки риска"
    )
