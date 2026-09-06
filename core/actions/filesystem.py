"""Файловая система (filesystem) — scoped user-document operations.

Инструменты для чтения/записи/поиска файлов в безопасной директории
(``settings.paths.documents_dir``). Доступ ко всей ФС НЕ предоставляется.

Инструменты:
- ``ListFilesTool`` — список файлов в директории.
- ``ReadFileTool`` — чтение файла.
- ``WriteFileTool`` — запись файла (создаёт родительские директории).
- ``SearchFilesTool`` — поиск файлов по содержимому/имени.
"""

from __future__ import annotations

import fnmatch
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.utils.logger import get_logger
from core.utils.paths import ensure_parent
from core.security.atomic import atomic_write_text

__all__ = [
    "resolve_docs_path",
    "list_files",
    "read_file",
    "write_file",
    "search_files",
    "ListFilesTool",
    "ReadFileTool",
    "WriteFileTool",
    "SearchFilesTool",
    "CopyFileTool",
    "MoveFileTool",
    "ListFilesRecursiveTool",
]

log = get_logger(__name__)

#: Максимальный размер читаемого файла (10 МБ)
_MAX_READ_SIZE = 10 * 1024 * 1024

#: Максимальное число результатов поиска
_MAX_SEARCH_RESULTS = 50

#: S6: сколько записей максимум просматривает одна операция поиска —
#: без потолка rglob по большой папке шёл без контроля до конца.
_MAX_SCANNED_ENTRIES = 5000

#: S6: бюджет времени одной операции поиска, секунд.
_SEARCH_TIME_BUDGET_SEC = 10.0

#: S6: лимит записи, применяемый когда настройка limits.max_write_bytes
#: отсутствует или непригодна (0, отрицательная, не число).
_FALLBACK_WRITE_LIMIT_BYTES = 2 * 1024 * 1024


def resolve_docs_path(path: str, settings: Settings) -> Path:
    """Преобразует относительный путь в абсолютный внутри documents_dir.

    Args:
        path: относительный путь (например, "notes/todo.txt").
        settings: конфигурация.

    Returns:
        Абсолютный Path внутри documents_dir.

    Raises:
        ValueError: если путь пытается выйти за пределы documents_dir.
    """
    docs_dir = settings.paths.resolved("documents_dir")
    docs_dir.mkdir(parents=True, exist_ok=True)

    target = (docs_dir / path).resolve()
    try:
        target.relative_to(docs_dir.resolve())
    except ValueError:
        raise ValueError(f"Путь '{path}' выходит за пределы documents_dir")

    return target


def _scope_root(scope: str, settings: Settings) -> Path:
    normalized = str(scope or "documents").strip().casefold()
    if normalized == "documents":
        root = settings.paths.resolved("documents_dir")
    elif normalized == "downloads":
        root = Path.home() / "Downloads"
    else:
        raise ValueError(f"Неизвестная область файлов: {scope}")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_read_path(path: str, settings: Settings, scope: str = "documents") -> Path:
    """Resolve a read-only path inside documents_dir or the user's Downloads."""
    requested = Path(str(path)).expanduser()
    target = requested.resolve() if requested.is_absolute() else (_scope_root(scope, settings) / requested).resolve()
    allowed_roots = (
        _scope_root("documents", settings),
        _scope_root("downloads", settings),
    )
    if not any(target == root or root in target.parents for root in allowed_roots):
        raise ValueError(f"Путь '{path}' выходит за пределы разрешённых пользовательских документов")
    return target


def list_files(dir_path: str, settings: Settings, recursive: bool = False) -> List[str]:
    """Возвращает список файлов в директории.

    Args:
        dir_path: относительный путь внутри documents_dir (пусто = корень).
        settings: конфигурация.
        recursive: рекурсивно обходить поддиректории.

    Returns:
        Список относительных путей к файлам.
    """
    base = resolve_docs_path(dir_path, settings)
    if not base.exists() or not base.is_dir():
        return []

    files: List[str] = []
    if recursive:
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    files.append(str(p.relative_to(settings.paths.resolved("documents_dir"))))
                except ValueError:
                    pass
    else:
        for p in base.iterdir():
            if p.is_file():
                try:
                    files.append(str(p.relative_to(settings.paths.resolved("documents_dir"))))
                except ValueError:
                    pass
    return sorted(files)


def read_file(path: str, settings: Settings, max_size: int = _MAX_READ_SIZE,
              scope: str = "documents") -> str:
    """Читает текстовый файл.

    Args:
        path: относительный путь внутри documents_dir.
        settings: конфигурация.
        max_size: максимальный размер файла в байтах.

    Returns:
        Содержимое файла (UTF-8, fallback cp1251).

    Raises:
        ValueError: если файл слишком большой или не найден.
    """
    target = resolve_read_path(path, settings, scope)
    if not target.is_file():
        raise ValueError(f"Файл не найден: {path}")

    size = target.stat().st_size
    if size > max_size:
        raise ValueError(f"Файл слишком большой: {size} байт (лимит {max_size})")

    if target.suffix.casefold() in {".pdf", ".docx", ".xlsx", ".pptx", ".text", ".md"}:
        from core.memory.document_rag import read_document
        return read_document(target)
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return target.read_text(encoding="cp1251", errors="replace")


def _write_limit(settings: Settings | None) -> int:
    """S6: лимит записи из настроек; непригодное значение — безопасный дефолт."""
    raw = getattr(getattr(settings, "limits", None), "max_write_bytes", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _FALLBACK_WRITE_LIMIT_BYTES
    return value if value > 0 else _FALLBACK_WRITE_LIMIT_BYTES


def write_file(path: str, content: str, settings: Settings) -> ActionResult:
    """Записывает файл (создаёт родительские директории).

    Args:
        path: относительный путь внутри documents_dir.
        content: текст для записи.
        settings: конфигурация.

    Returns:
        ActionResult с ok=True при успехе.
    """
    try:
        target = resolve_docs_path(path, settings)
        # S6: отказ ДО записи — неудачная гигантская запись не должна
        # успевать создать/перезаписать файл, прежде чем её отвергнут.
        limit = _write_limit(settings)
        size = len(content.encode("utf-8"))
        if size > limit:
            raise ValueError(
                f"объём {size} байт превышает лимит записи {limit} байт "
                f"(limits.max_write_bytes)"
            )
        ensure_parent(target)
        atomic_write_text(target, content)
        log.info("Файл записан: %s (%d байт)", path, len(content.encode("utf-8")))
        return ActionResult(
            tool="write_file",
            args={"path": path, "size": len(content)},
            ok=True,
            output=f"Файл сохранён: {path} ({len(content)} символов)",
        )
    except Exception as exc:
        log.error("Ошибка записи файла %s: %s", path, exc)
        return ActionResult(
            tool="write_file",
            args={"path": path},
            ok=False,
            error=f"Не удалось записать файл: {exc}",
        )


def copy_file(source: str, destination: str, settings: Settings) -> ActionResult:
    """Copies a file inside documents_dir; destination parents are created."""
    try:
        src = resolve_docs_path(source, settings)
        dst = resolve_docs_path(destination, settings)
        if not src.is_file():
            raise ValueError(f"Файл не найден: {source}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return ActionResult("file_copy", {"source": source, "destination": destination},
                            True, f"Скопировано: {source} -> {destination}")
    except Exception as exc:
        return ActionResult("file_copy", {"source": source, "destination": destination},
                            False, error=f"Копирование не выполнено: {exc}")


def move_file(source: str, destination: str, settings: Settings) -> ActionResult:
    """Moves a file inside documents_dir; path traversal remains blocked."""
    try:
        src = resolve_docs_path(source, settings)
        dst = resolve_docs_path(destination, settings)
        if not src.is_file():
            raise ValueError(f"Файл не найден: {source}")
        if dst.exists():
            raise ValueError(f"Назначение уже существует: {destination}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ActionResult("file_move", {"source": source, "destination": destination},
                            True, f"Перемещено: {source} -> {destination}")
    except Exception as exc:
        return ActionResult("file_move", {"source": source, "destination": destination},
                            False, error=f"Перемещение не выполнено: {exc}")


def search_files(
    query: str,
    settings: Settings,
    dir_path: str = "",
    max_results: int = _MAX_SEARCH_RESULTS,
    scope: str = "documents",
    sort_by: str = "path",
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """Ищет файлы по имени или содержимому (простой текстовый поиск).

    Args:
        query: поисковая строка.
        settings: конфигурация.
        dir_path: директория для поиска (относительно documents_dir).
        max_results: максимум результатов.
        cancel_event: S6 — сигнал отмены от executor'а; проверяется на
            каждой итерации, прерванный поиск возвращает найденное к этому
            моменту.

    Returns:
        Список относительных путей к найденным файлам.
    """
    if not query or not query.strip():
        return []

    root = _scope_root(scope, settings)
    base = resolve_read_path(dir_path, settings, scope)
    if not base.exists():
        return []

    query_lower = query.lower()
    matches: List[Path] = []
    # S6: обход ограничен числом просмотренных записей и временем; без
    # потолков rglob по большой папке шёл минуты и не реагировал на отмену.
    deadline = time.monotonic() + _SEARCH_TIME_BUDGET_SEC
    scanned = 0
    for p in base.rglob("*"):
        scanned += 1
        if scanned > _MAX_SCANNED_ENTRIES:
            log.info("Поиск «%s» остановлен: просмотрено %d записей (лимит %d)",
                     query, scanned - 1, _MAX_SCANNED_ENTRIES)
            break
        if cancel_event is not None and cancel_event.is_set():
            log.info("Поиск «%s» прерван по отмене после %d записей", query, scanned - 1)
            break
        if time.monotonic() > deadline:
            log.info("Поиск «%s» остановлен по бюджету времени (%.0f с) после %d записей",
                     query, _SEARCH_TIME_BUDGET_SEC, scanned - 1)
            break

        if not p.is_file():
            continue

        # Поиск по имени
        if query_lower in p.name.lower():
            try:
                matches.append(p)
                continue
            except ValueError:
                pass

        # Поиск по содержимому (только текстовые файлы, до 1 МБ)
        if p.stat().st_size > 1024 * 1024:
            continue
        if p.suffix.lower() not in (".txt", ".md", ".py", ".json", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".log", ".csv"):
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if query_lower in text.lower():
                matches.append(p)
        except Exception:
            pass
    if str(sort_by).casefold() == "modified_desc":
        matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    else:
        matches.sort(key=lambda item: str(item).casefold())
    limited = matches[:max_results]
    if str(scope).casefold() == "downloads":
        return [str(item) for item in limited]
    return [str(item.relative_to(root)) for item in limited]


# --------------------------------------------------------------------------- #
# Tool-обёртки
# --------------------------------------------------------------------------- #


class ListFilesTool(Tool):
    """Инструмент: список файлов в директории."""

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "Возвращает список файлов в указанной директории внутри documents_dir. "
            "Можно включить рекурсивный обход поддиректорий."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dir_path": {
                    "type": "string",
                    "description": "Относительный путь к директории (пусто = корень documents_dir).",
                    "default": "",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Рекурсивно обходить поддиректории.",
                    "default": False,
                },
            },
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        dir_path = args.get("dir_path", "")
        recursive = args.get("recursive", False)

        files = list_files(dir_path, context.settings, recursive)
        if not files:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=True,
                output=f"Директория '{dir_path or '(корень)'}' пуста или не существует.",
            )

        lines = [f"Файлы в '{dir_path or '(корень)'}':"]
        for f in files:
            lines.append(f"  📄 {f}")
        return ActionResult(
            tool=self.name,
            args=args,
            ok=True,
            output="\n".join(lines),
        )


class ReadFileTool(Tool):
    """Инструмент: чтение файла."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Читает текстовый файл из documents_dir. Поддерживает UTF-8 и cp1251. "
            "Максимальный размер файла — 10 МБ."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Относительный путь к файлу внутри documents_dir.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["documents", "downloads"],
                    "default": "documents",
                    "description": "Область чтения: documents или папка Downloads пользователя.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        path = args["path"]
        try:
            content = read_file(path, context.settings, scope=args.get("scope", "documents"))
            # §22 — содержимое файла это внешние ДАННЫЕ, не команды.
            # Оборачиваем на границе инструмент→модель. S6: контент больше не
            # режется ни здесь, ни в конверте (санитизация и detect_injection
            # применяются к полному тексту) — границу контекста модели ставит
            # честное усечение executor'а («вывод усечён: N символов, потолок M»).
            from core.safety import wrap_untrusted
            wrapped = wrap_untrusted(
                content, source=f"read_file ({path})",
                max_chars=max(len(content), 1),
            )
            return ActionResult(
                tool=self.name,
                args=args,
                ok=True,
                output=f"Содержимое {path}:\n\n{wrapped}",
            )
        except Exception as exc:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=False,
                error=f"Не удалось прочитать файл: {exc}",
            )


class WriteFileTool(Tool):
    """Инструмент: запись файла."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Создаёт или перезаписывает текстовый файл в documents_dir. "
            "Автоматически создаёт недостающие родительские директории."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Относительный путь к файлу внутри documents_dir.",
                },
                "content": {
                    "type": "string",
                    "description": "Текст для записи в файл.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        return write_file(args["path"], args["content"], context.settings)


class SearchFilesTool(Tool):
    """Инструмент: поиск файлов по имени или содержимому."""

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return (
            "Ищет файлы в documents_dir или пользовательской Downloads по имени/содержимому. "
            "Может сортировать по времени изменения, чтобы найти последний файл. "
            "По содержимому ищет только в текстовых файлах (.txt, .md, .py, .json и др.) до 1 МБ."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (подстрока в имени или тексте файла).",
                },
                "dir_path": {
                    "type": "string",
                    "description": "Директория для поиска (относительно documents_dir, пусто = везде).",
                    "default": "",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Максимальное число результатов.",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
                "scope": {
                    "type": "string",
                    "enum": ["documents", "downloads"],
                    "default": "documents",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["path", "modified_desc"],
                    "default": "path",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        query = args["query"]
        dir_path = args.get("dir_path", "")
        max_results = args.get("max_results", _MAX_SEARCH_RESULTS)

        scope = args.get("scope", "documents")
        sort_by = args.get("sort_by", "path")
        results = search_files(
            query, context.settings, dir_path, max_results,
            scope=scope, sort_by=sort_by,
            cancel_event=context.cancel_event,
        )

        if not results:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=True,
                output=f"Файлы по запросу «{query}» не найдены.",
            )

        lines = [f"Найдено файлов по «{query}» ({len(results)}), scope={scope}, sort={sort_by}:"]
        for f in results:
            target = resolve_read_path(f, context.settings, scope)
            lines.append(
                f"  FILE {target} | modified={target.stat().st_mtime_ns} | size={target.stat().st_size}"
            )
        return ActionResult(
            tool=self.name,
            args=args,
            ok=True,
            output="\n".join(lines),
        )


class CopyFileTool(Tool):
    @property
    def name(self) -> str: return "file_copy"
    @property
    def description(self) -> str: return "Копирует файл внутри documents_dir с проверкой границ пути."
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {
            "source": {"type": "string"}, "destination": {"type": "string"}},
            "required": ["source", "destination"], "additionalProperties": False}
    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        return copy_file(args["source"], args["destination"], context.settings)


class MoveFileTool(Tool):
    @property
    def name(self) -> str: return "file_move"
    @property
    def description(self) -> str: return "Перемещает файл внутри documents_dir с проверкой границ пути."
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {
            "source": {"type": "string"}, "destination": {"type": "string"}},
            "required": ["source", "destination"], "additionalProperties": False}
    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        return move_file(args["source"], args["destination"], context.settings)


class ListFilesRecursiveTool(ListFilesTool):
    @property
    def name(self) -> str: return "list_files_recursive"
    @property
    def description(self) -> str: return "Рекурсивно перечисляет файлы внутри documents_dir для проверки состояния."
    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        return super().run({"dir_path": args.get("dir_path", ""), "recursive": True}, context)


# Авто-регистрация
DEFAULT_REGISTRY.register(ListFilesTool())
DEFAULT_REGISTRY.register(ReadFileTool())
DEFAULT_REGISTRY.register(WriteFileTool())
DEFAULT_REGISTRY.register(SearchFilesTool())
DEFAULT_REGISTRY.register(CopyFileTool())
DEFAULT_REGISTRY.register(MoveFileTool())
DEFAULT_REGISTRY.register(ListFilesRecursiveTool())
