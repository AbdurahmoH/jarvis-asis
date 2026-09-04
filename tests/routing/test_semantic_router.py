"""Тесты для изолированного модуля семантической маршрутизации (core/routing/semantic_router.py)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from core.routing.semantic_router import RoutingContext, SemanticRouter, assess_risk


@pytest.fixture(scope="module")
def router() -> SemanticRouter:
    r = SemanticRouter()
    r.ensure_index()
    return r


def test_canonical_shortcuts(router: SemanticRouter):
    """Шорткаты должны срабатывать полнофразово, без вызова эмбеддера и с максимальной уверенностью."""
    shortcuts = [
        ("громче", "action", "volume"),
        ("тише", "action", "volume"),
        ("который час", "action", "current_time"),
        ("статус системы", "action", "system_status"),
        ("курс доллара", "fresh_data", "public_data"),
        ("сделай скриншот", "action", "computer_screenshot"),
    ]
    for phrase, expected_kind, expected_tool in shortcuts:
        dec = router.route(phrase, RoutingContext(llm_available=False))
        assert dec.tier == "shortcut", f"Фраза '{phrase}' должна быть tier='shortcut'"
        assert dec.kind == expected_kind
        assert dec.tool == expected_tool
        assert dec.confidence == 1.0


def test_risk_escalation_zero_fn():
    """Проверка независимого гейта безопасности: критические и деструктивные операции требуют подтверждения."""
    critical_samples = [
        ("удали всё из папки temp", True),
        ("отформатируй флешку в fat32", True),
        ("перезапиши системный файл hosts пустым содержимым", True),
        ("убей все процессы python в диспетчере задач", True),
        ("отключи брандмауэр windows", True),
        ("отправь файл с паролями на почту test@example.com", True),
        ("снеси все файлы с расширением log в корне диска C", True),
        ("очисти корзину подчистую прямо сейчас", True),
        ("выключи компьютер немедленно", True),
        ("пока меня нет, разбери загрузки по папкам и напиши что сделал", True),
    ]
    for text, expected_needs_conf in critical_samples:
        risk_level, needs_conf = assess_risk(None, None, text)
        assert needs_conf == expected_needs_conf, f"Фраза '{text}' должна требовать подтверждения (needs_confirmation=True), получено {needs_conf}"
        assert risk_level in ("high", "critical"), f"Фраза '{text}' должна иметь уровень high/critical, получено {risk_level}"


def test_semantic_routing_intents(router: SemanticRouter):
    """Семантическая классификация намерений (действие, диалог, вопрос, миссия)."""
    cases = [
        ("открой эксель", "action"),
        ("привет, как твои дела сегодня?", "chat"),
        ("объясни на простом примере что такое рекурсия", "question"),
        ("подготовь подробную презентацию для инвесторов на 15 слайдов с графиками", "mission"),
    ]
    for text, expected_kind in cases:
        dec = router.route(text, RoutingContext(llm_available=False))
        assert dec.kind == expected_kind, f"Для фpath '{text}' ожидался kind='{expected_kind}', получен '{dec.kind}'"


def test_thresholds_file_exists():
    """Файл core/routing/thresholds.json должен существовать и содержать откалиброванные значения."""
    path = Path("core/routing/thresholds.json")
    assert path.exists(), "thresholds.json не найден"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "confidence_threshold" in data
    assert "margin_threshold" in data
    assert data["dev_kind_accuracy"] >= 0.85
    assert data["dev_high_risk_fn"] == 0
