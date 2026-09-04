"""Тест защиты от утечки обучающих/паспортных данных в eval-сет (Data Leakage Protection).

Проверяет, что ни одна фраза (в нормализованном виде) из eval-сета не присутствует
в примерах возможностей (examples_ru), контр-примерах (counter_examples_ru)
или семантических якорях (anchors_ru).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.routing.anchors_ru import ANCHORS_BY_KIND
from core.routing.capability_examples_ru import (
    CAPABILITY_COUNTER_EXAMPLES_RU,
    CAPABILITY_EXAMPLES_RU,
)


def _normalize(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    return " ".join(cleaned.split())


def test_no_overlap_between_eval_set_and_router_data():
    eval_set_path = Path("tests/routing/routing_eval_set.json")
    assert eval_set_path.exists(), f"Файл датасета {eval_set_path} не найден"

    with open(eval_set_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    eval_normalized = {_normalize(it["text"]) for it in items}
    assert len(eval_normalized) >= 120, "Датасет должен содержать минимум 120 уникальных фраз"

    # Сбор всех примеров из паспортов и якорей
    router_corpus: set[str] = set()

    for tool_name, examples in CAPABILITY_EXAMPLES_RU.items():
        for ex in examples:
            router_corpus.add(_normalize(ex))

    for tool_name, counter_examples in CAPABILITY_COUNTER_EXAMPLES_RU.items():
        for cex in counter_examples:
            router_corpus.add(_normalize(cex))

    for kind, anchors in ANCHORS_BY_KIND.items():
        for anchor in anchors:
            router_corpus.add(_normalize(anchor))

    overlap = eval_normalized.intersection(router_corpus)

    assert not overlap, (
        f"ОБНАРУЖЕНА УТЕЧКА: {len(overlap)} фраз из eval_set пересекаются с паспортами/якорями:\n"
        + "\n".join(f"  - {phrase}" for phrase in sorted(overlap))
    )
