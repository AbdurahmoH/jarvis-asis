"""Regression S6 — файловые инструменты в пределах и с честными усечениями.

Аудит 2026-09-05 нашёл три дыры в ``core/actions/filesystem.py``:

1. ``ReadFileTool`` резал контент до 1000 символов при лимите чтения
   10 МБ — «перескажи документ» получал тишину: модель видела усечённый
   текст без внятной границы, а честное усечение executor'а никогда не
   срабатывало, потому что инструмент ужимался сам.
2. ``search_files`` делал неограниченный ``rglob("*")`` без проверки
   ``cancel_event`` — поиск по большой папке шёл минуты и не останавливался
   ни отменой, ни ничем.
3. ``write_file`` не имел лимита объёма — модель могла записать произвольный
   объём на диск.

Теперь: инструмент отдаёт полный контент, границу контекста модели ставит
честное усечение executor'а («вывод усечён: N символов, потолок M»);
поиск ограничен числом записей, бюджетом времени и отменой; запись
ограничена ``limits.max_write_bytes`` с отказом ДО создания файла.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from config.settings import Settings
from core.actions.base import ToolContext
from core.actions.executor import execute_tool
from core.actions.filesystem import (
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
    _FALLBACK_WRITE_LIMIT_BYTES,
    search_files,
)
from core.actions.registry import ToolRegistry


@pytest.fixture()
def docs_settings(tmp_path) -> Settings:
    """Settings с documents_dir во временной папке."""
    return Settings(paths={"documents_dir": str(tmp_path / "docs")})


def _context(settings: Settings, **extra: Any) -> ToolContext:
    return ToolContext(settings=settings, **extra)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(SearchFilesTool())
    return registry


# --------------------------------------------------------------------------- #
# read_file: полный контент, честное усечение на границе executor'а
# --------------------------------------------------------------------------- #


def test_read_file_returns_full_content(docs_settings: Settings,
                                        tmp_path) -> None:
    """50 КБ доезжают целиком — обрезки на 1000 символов больше нет."""
    body = "а" * 49_000 + "ХВОСТ-МАРКЕР-КОНЕЦ"
    target = tmp_path / "docs" / "big.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    result = ReadFileTool().run({"path": "big.txt"}, _context(docs_settings))

    assert result.ok
    assert "ХВОСТ-МАРКЕР-КОНЕЦ" in str(result.output)
    assert "обрезано" not in str(result.output)


def test_executor_truncates_large_output_honestly(docs_settings: Settings,
                                                  tmp_path) -> None:
    """Граница контекста модели — executor, с честными числами N из M."""
    import re

    body = "б" * 200_000
    target = tmp_path / "docs" / "huge.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    result = execute_tool(
        _registry(), "read_file", {"path": "huge.txt"},
        _context(docs_settings), timeout_sec=10.0,
    )

    assert result.ok
    output = str(result.output)
    assert "вывод усечён" in output
    match = re.search(r"вывод усечён: (\d+) символов", output)
    assert match, "пометка называет исходную длину"
    assert int(match.group(1)) >= len(body)


def test_read_file_50kb_file_passes_whole(docs_settings: Settings,
                                          tmp_path) -> None:
    """Файл в пределах байтового потолка executor'а доезжает целиком.

    Потолок считается в байтах UTF-8, поэтому тело ASCII: кириллица
    двухбайтная и удвоила бы объём.
    """
    body = "b" * 50_000
    target = tmp_path / "docs" / "fit.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    result = execute_tool(
        _registry(), "read_file", {"path": "fit.txt"},
        _context(docs_settings), timeout_sec=10.0,
    )

    assert result.ok
    output = str(result.output)
    assert "вывод усечён" not in output
    assert body in output


# --------------------------------------------------------------------------- #
# search_files: отмена и пределы просмотра
# --------------------------------------------------------------------------- #


def _make_matches(docs_settings: Settings, count: int) -> None:
    docs = docs_settings.paths.resolved("documents_dir")
    docs.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (docs / f"заметка_{i}.txt").write_text("мягкий кот ищет дом", encoding="utf-8")


def test_search_files_stops_on_cancel(docs_settings: Settings) -> None:
    _make_matches(docs_settings, 3)
    cancel = threading.Event()
    cancel.set()

    results = search_files("кот", docs_settings, cancel_event=cancel)

    assert results == [], "отменённый поиск не продолжает обход"


def test_search_files_default_is_unlimited(docs_settings: Settings) -> None:
    """Без отмены — прежнее поведение, все совпадения находятся."""
    _make_matches(docs_settings, 5)

    results = search_files("кот", docs_settings)

    assert len(results) == 5


def test_search_files_scan_limit_stops_early(docs_settings: Settings,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Лимит просмотра: поиск останавливается раньше конца каталога."""
    import core.actions.filesystem as fs_module
    _make_matches(docs_settings, 5)
    monkeypatch.setattr(fs_module, "_MAX_SCANNED_ENTRIES", 2)

    results = search_files("кот", docs_settings)

    assert 0 < len(results) < 5, "обход прерван лимитом записей"


def test_search_files_time_budget_stops_early(docs_settings: Settings,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import core.actions.filesystem as fs_module
    _make_matches(docs_settings, 5)
    # Отрицательный бюджет: первая же проверка дедлайна обязательна истинна,
    # поэтому тест не зависит от разрешения системных часов.
    monkeypatch.setattr(fs_module, "_SEARCH_TIME_BUDGET_SEC", -1.0)

    results = search_files("кот", docs_settings)

    assert len(results) < 5, "обход прерван бюджетом времени"


def test_search_tool_passes_cancel_event(docs_settings: Settings) -> None:
    """Инструмент передаёт cancel_event контекста в функцию поиска."""
    _make_matches(docs_settings, 3)
    cancel = threading.Event()
    cancel.set()

    result = SearchFilesTool().run(
        {"query": "кот"}, _context(docs_settings, cancel_event=cancel),
    )

    assert result.ok
    assert "не найдены" in str(result.output)


# --------------------------------------------------------------------------- #
# write_file: лимит объёма
# --------------------------------------------------------------------------- #


def test_write_file_rejects_oversized_content(tmp_path) -> None:
    settings = Settings(
        paths={"documents_dir": str(tmp_path / "docs")},
        limits={"max_write_bytes": 100},
    )

    result = WriteFileTool().run(
        {"path": "big.txt", "content": "г" * 500},
        _context(settings),
    )

    assert not result.ok
    assert "лимит" in str(result.error)
    assert not (tmp_path / "docs" / "big.txt").exists(), (
        "отказ происходит ДО записи — файл не должен создаваться"
    )


def test_write_file_accepts_content_within_limit(tmp_path) -> None:
    settings = Settings(
        paths={"documents_dir": str(tmp_path / "docs")},
        limits={"max_write_bytes": 1000},
    )

    result = WriteFileTool().run(
        {"path": "ok.txt", "content": "д" * 100},
        _context(settings),
    )

    assert result.ok
    assert (tmp_path / "docs" / "ok.txt").read_text(encoding="utf-8") == "д" * 100


def test_write_limit_falls_back_when_setting_is_unusable(
        docs_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Непригодная настройка (0/не число) деградирует к дефолту модуля."""
    from core.actions import filesystem as fs_module

    usable = SimpleNamespace(paths=docs_settings.paths,
                             limits=SimpleNamespace(max_write_bytes=0))
    assert fs_module._write_limit(usable) == _FALLBACK_WRITE_LIMIT_BYTES

    garbage = SimpleNamespace(paths=docs_settings.paths,
                              limits=SimpleNamespace(max_write_bytes="мусор"))
    assert fs_module._write_limit(garbage) == _FALLBACK_WRITE_LIMIT_BYTES

    none_limits = SimpleNamespace(paths=docs_settings.paths, limits=None)
    assert fs_module._write_limit(none_limits) == _FALLBACK_WRITE_LIMIT_BYTES


def test_settings_reject_nonpositive_write_limit() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(limits={"max_write_bytes": 0})


# --------------------------------------------------------------------------- #
# agent: честная пометка на границе промпта
# --------------------------------------------------------------------------- #


def test_goal_observation_compaction_is_honest() -> None:
    from core.agent import Agent

    long_output = "е" * 9000
    serialized_len = len(json.dumps(long_output, ensure_ascii=False))
    compact = Agent._compact_goal_observations([
        {"step": 1, "tool": "read_file", "arguments": {},
         "output": long_output, "error": None, "verification": None},
    ])

    text = compact[0]["output"]
    assert "показаны первые 5000 из" in text
    assert str(serialized_len) in text
    assert "<truncated>" not in text
