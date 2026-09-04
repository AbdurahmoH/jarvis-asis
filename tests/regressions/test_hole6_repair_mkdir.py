"""Regression: ДЫРА 6 — _heal_path_args не должна создавать директории
вне разрешённых scope-корней (documents_dir / Downloads).

До фикса: p.parent.mkdir(parents=True, exist_ok=True) вызывается для любого
абсолютного пути без проверки scope — side effect без подтверждения.

После фикса: mkdir не вызывается вообще (путь недоступен = ошибка, не починка).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.repair import RepairLoop


def test_heal_path_args_does_not_mkdir_outside_scope(tmp_path):
    """_heal_path_args не создаёт директории для произвольных абсолютных путей."""
    # Путь вне documents_dir / Downloads
    outside_dir = tmp_path / "secret_dir" / "subdir"
    outside_file = outside_dir / "file.txt"

    assert not outside_dir.exists(), "Директория не должна существовать до вызова"

    args = {"path": str(outside_file)}
    healed = RepairLoop._heal_path_args(args)

    # После фикса: директория НЕ создаётся
    assert not outside_dir.exists(), (
        f"_heal_path_args создала директорию {outside_dir} без подтверждения пользователя"
    )


def test_heal_path_args_does_not_mkdir_system_paths():
    """_heal_path_args не создаёт системные директории."""
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    # Путь в системной директории (не существует, но попытка mkdir опасна)
    system_path = r"C:\Windows\Temp\jarvis_test_should_not_exist\file.txt"
    args = {"path": system_path}

    # Не должно бросать исключений и не должно создавать директории
    healed = RepairLoop._heal_path_args(args)

    test_dir = Path(r"C:\Windows\Temp\jarvis_test_should_not_exist")
    if test_dir.exists():
        # Если создалась — это баг, убираем за собой
        try:
            test_dir.rmdir()
        except Exception:
            pass
        pytest.fail(
            "_heal_path_args создала системную директорию C:\\Windows\\Temp\\jarvis_test_should_not_exist"
        )


def test_heal_path_args_returns_unchanged_for_nonexistent_absolute(tmp_path):
    """Для несуществующего абсолютного пути аргументы возвращаются без изменений."""
    nonexistent = tmp_path / "does_not_exist" / "file.txt"
    args = {"path": str(nonexistent)}

    healed = RepairLoop._heal_path_args(args)

    # После фикса: путь не изменяется (нет mkdir, нет модификации)
    assert healed["path"] == str(nonexistent), (
        "Путь не должен изменяться если директория не создаётся"
    )
    assert not nonexistent.parent.exists(), (
        "Родительская директория не должна быть создана"
    )
