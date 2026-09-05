"""Regression S0 — живые учётные данные не лежат в дереве и не попадают в git.

Найдено при аудите 2026-09-05: в корне репозитория лежали одноразовые
отладочные скрипты ``test_ws.cjs`` и ``test_running_ws.cjs`` с зашитым живым
GitLab PAT. Файлы были untracked, но НЕ покрыты ``.gitignore`` — один
``git add .`` опубликовал бы токен. Токен отозван владельцем, файлы удалены.

Тест закрепляет три свойства:

1. Скрипты-однодневки с токеном отсутствуют в дереве.
2. Корневые ``*.cjs``/``*.mjs`` и локальные базы игнорируются git — то есть
   повторное появление такого файла не может быть закоммичено случайно.
3. Ни в одном отслеживаемом файле нет строк, похожих на живые токены.

Литералы префиксов собираются конкатенацией, чтобы сам сканер не содержал
искомых образцов и не давал ложного срабатывания на себе.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Расширения, которые имеет смысл читать как текст.
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".json",
    ".md", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".rs", ".ps1",
    ".sh", ".bat", ".html", ".css", ".env", ".spec",
}

#: Файл больше этого размера не читаем (данные/фикстуры, не исходники).
_MAX_SCAN_BYTES = 512 * 1024

#: Образцы живых учётных данных. Префиксы собраны конкатенацией намеренно.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitLab PAT", re.compile("gl" + r"pat-[A-Za-z0-9_\-]{20,}")),
    ("GitHub PAT (classic)", re.compile("gh" + r"p_[A-Za-z0-9]{36}")),
    ("GitHub PAT (fine-grained)", re.compile("github" + r"_pat_[A-Za-z0-9_]{50,}")),
    ("Anthropic API key", re.compile("sk-" + r"ant-[A-Za-z0-9_\-]{20,}")),
    ("AWS access key id", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("Slack token", re.compile("xo" + r"x[baprs]-[A-Za-z0-9\-]{10,}")),
    # PEM-заголовка недостаточно: в tests/test_hardening_redaction.py лежит
    # синтетическая строка "…PRIVATE KEY-----abc-----END…" как фикстура для
    # проверки редактирования. Настоящий ключ — это заголовок, перевод строки
    # и сотни символов base64-тела; на такой шаблон фикстура не попадает.
    ("private key block", re.compile(
        "-----BEGIN" + r"[ A-Z]* PRIVATE KEY-----[ \t]*\r?\n[A-Za-z0-9+/=\s]{100,}"
    )),
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=False,
    )


def _git_available() -> bool:
    try:
        return _git("rev-parse", "--git-dir").returncode == 0
    except OSError:
        return False


def test_token_scratch_files_absent() -> None:
    """Скрипты с зашитым GitLab-токеном удалены из дерева."""
    for name in ("test_ws.cjs", "test_running_ws.cjs"):
        assert not (_REPO_ROOT / name).exists(), (
            f"{name} снова появился в корне — в нём был живой GitLab PAT"
        )


@pytest.mark.parametrize(
    "probe",
    ["test_ws.cjs", "scratch.cjs", "scratch.mjs", "elsewhere/local.db", "cache.sqlite3"],
)
def test_scratch_and_database_paths_are_gitignored(probe: str) -> None:
    """Корневые отладочные скрипты и локальные базы игнорируются git."""
    if not _git_available():
        pytest.skip("git недоступен в этом окружении")
    done = _git("check-ignore", "-q", probe)
    assert done.returncode == 0, f"{probe} НЕ игнорируется git — может уйти в коммит"


def test_no_live_credentials_in_tracked_files() -> None:
    """Ни один отслеживаемый файл не содержит строку, похожую на живой токен."""
    if not _git_available():
        pytest.skip("git недоступен в этом окружении")
    listing = _git("ls-files", "-z")
    assert listing.returncode == 0, f"git ls-files не отработал: {listing.stderr}"

    findings: list[str] = []
    for entry in listing.stdout.split("\0"):
        if not entry:
            continue
        path = _REPO_ROOT / entry
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in _SECRET_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{entry}:{line} — {label}")

    assert not findings, "Похожие на живые учётные данные строки:\n" + "\n".join(findings)
