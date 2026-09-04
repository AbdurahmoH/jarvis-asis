"""Блокирующий тест качества semantic-маршрутизации (шаг 8).

Роутер core/routing/semantic_router — единственная точка решения.
Тест падает, если на dev-сплите kind accuracy < 85% или есть хотя бы
один пропущенный high-risk запрос (FN > 0).

Запуск: python -m pytest tests/routing/test_routing_eval.py -o addopts= -q
(~30 c: строит/читает векторный индекс роутера).
"""

from __future__ import annotations

from pathlib import Path

from scripts.routing_eval import run_evaluation

DATASET_PATH = Path("tests/routing/routing_eval_set.json")

MIN_DEV_KIND_ACCURACY = 0.85
MAX_HIGH_RISK_FN = 0


def test_routing_semantic_blocking_gate():
    """Блокирующий гейт: dev kind >= 85%, FN high-risk == 0, wrong substitution == 0."""
    assert DATASET_PATH.exists(), f"Датасет {DATASET_PATH} не найден"

    _, summary = run_evaluation(
        str(DATASET_PATH), router_mode="semantic", split="dev", llm_available=False,
    )

    assert summary["kind_accuracy"] >= MIN_DEV_KIND_ACCURACY, (
        f"Kind accuracy на dev ниже блокирующего порога: "
        f"{summary['kind_accuracy']:.4f} < {MIN_DEV_KIND_ACCURACY}"
    )
    assert summary["high_risk_fn"] <= MAX_HIGH_RISK_FN, (
        f"Пропущены high-risk запросы (FN): {summary['high_risk_fn']} > {MAX_HIGH_RISK_FN}"
    )
    assert summary["unsupported_wrong_sub_count"] == 0, (
        "Неподдерживаемый запрос подменён похожим инструментом: "
        f"{summary['unsupported_wrong_sub_count']} != 0"
    )
