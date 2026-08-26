"""Verifier — фактическая проверка результата (§14, §16).

J.A.R.V.I.S. НИКОГДА не считает задачу выполненной только потому, что вызов
инструмента завершился без исключения. Нужны ФАКТИЧЕСКИЕ проверки:

    write_file   -> файл существует на диске и читается?
    read_file    -> контент реально прочитан?
    open_app     -> процесс с таким именем реально появился?
    close_app    -> процесс реально исчез?
    web_fetch    -> страница реально загрузилась (есть контент)?
    web_search   -> есть непустые результаты?
    system_status-> метрики реально получены?
    add_reminder -> напоминание реально в списке?

Реализуется как реестр verifier-ов по имени инструмента. Инструмент (или
этот модуль) регистрирует функцию проверки через ``register_verifier``.

Если для инструмента нет специализированной проверки — используется
``default_verify``: она НЕ утверждает, что проверка была настоящей, и
помечает результат ``method="trust_ok"`` + ``strict=False``, чтобы агент
и отчёты честно различали «проверено фактически» и «доверились ok».

ВАЖНО (§14): ``VerificationResult.verified`` — единственный признак, по
которому агент имеет право сказать «готово».
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.actions.base import ActionResult
from core.utils.logger import get_logger

__all__ = [
    "VerificationResult",
    "verify_action_result",
    "register_verifier",
    "default_verify",
    "verify_file_exists",
    "verify_command_exit",
    "has_strict_verifier",
    "list_verified_tools",
]

log = get_logger(__name__)


@dataclass
class VerificationResult:
    """Результат фактической проверки действия (§14).

    Attributes:
        verified: прошла ли проверка.
        method: каким способом проверяли ("file_exists", "process_running", ...).
        detail: человекочитаемая деталь для отчёта.
        strict: True — это была НАСТОЯЩАЯ фактическая проверка;
            False — мы лишь доверились ``ok`` (нет специализированного verifier).
    """

    verified: bool
    method: str
    detail: str = ""
    strict: bool = True
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "method": self.method,
            "detail": self.detail,
            "strict": self.strict,
        }

    def __bool__(self) -> bool:
        return self.verified


# Реестр специализированных verifier-ов: имя инструмента -> callable.
_VERIFIERS: Dict[str, Callable[[ActionResult], VerificationResult]] = {}


def register_verifier(tool_name: str,
                      verifier: Callable[[ActionResult], VerificationResult]) -> None:
    """Регистрирует фактическую проверку для инструмента (§14)."""
    _VERIFIERS[tool_name] = verifier
    log.debug("Зарегистрирован verifier для инструмента '%s'", tool_name)


def has_strict_verifier(tool_name: str) -> bool:
    """Есть ли для инструмента настоящая (не trust_ok) проверка."""
    return tool_name in _VERIFIERS


def list_verified_tools() -> List[str]:
    """Список инструментов с фактическими проверками."""
    return sorted(_VERIFIERS.keys())


# --------------------------------------------------------------------------- #
#  Утилиты извлечения данных из ActionResult
# --------------------------------------------------------------------------- #

def _output_text(result: ActionResult) -> str:
    """Текст output инструмента (ActionResult — dataclass, не dict)."""
    out = getattr(result, "output", None)
    if out is None:
        return ""
    return str(out)


_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\s\"'<>|?*]+")
_PID_RE = re.compile(r"\bpid\s*=\s*(\d+)\b", re.IGNORECASE)


def _extract_paths(text: str) -> List[str]:
    """Достаёт похожие на пути токены из текста output."""
    found = [m.group(0).rstrip(".,;:)\"'") for m in _PATH_RE.finditer(text)]
    return found


# --------------------------------------------------------------------------- #
#  Универсальные verifier-ы
# --------------------------------------------------------------------------- #

def verify_file_exists(result: ActionResult) -> VerificationResult:
    """Проверяет, что файл из output реально существует на диске и читается."""
    if not result.ok:
        return VerificationResult(False, "file_exists", result.error or "ok=False")

    text = _output_text(result)
    candidates = _extract_paths(text)

    # Аргументы тоже могут содержать путь (например write_file(path=...)).
    for key in ("path", "file", "filename", "target", "dest"):
        val = (result.args or {}).get(key)
        if isinstance(val, str) and val.strip():
            candidates.append(val.strip())

    checked: List[str] = []
    for cand in candidates:
        p = Path(cand).expanduser()
        checked.append(str(p))
        if p.exists() and p.is_file():
            try:
                with open(p, "rb") as fh:
                    fh.read(1)
                return VerificationResult(True, "file_exists", f"файл существует и читается: {p}")
            except OSError as exc:
                return VerificationResult(False, "file_readable", f"{p}: {exc}")

    # Путь может быть относительным внутри documents_dir — ищем там.
    for cand in list(candidates):
        try:
            from core.utils.paths import PROJECT_ROOT
            p = PROJECT_ROOT / "data" / "documents" / Path(cand).name
            if p.is_file():
                return VerificationResult(True, "file_exists", f"файл найден в documents: {p}")
        except Exception:  # pragma: no cover — защитный путь
            pass

    if checked:
        return VerificationResult(False, "file_exists", f"файл не найден: {checked[:3]}")
    return VerificationResult(False, "no_path_in_output",
                              "в output нет пути к файлу — проверить нечем")


def verify_command_exit(result: ActionResult) -> VerificationResult:
    """Проверка по exit code, если инструмент его сообщил."""
    text = _output_text(result).lower()
    m = re.search(r"exit_code[=:]?\s*(\d+)", text)
    if m:
        code = int(m.group(1))
        return VerificationResult(code == 0, "exit_code", f"returncode={code}")
    if result.ok:
        return VerificationResult(False, "started_no_error",
                                  "процесс запущен без исключения", strict=False)
    return VerificationResult(False, "exit_code", result.error or "ok=False")


def _process_matches(names: List[str]) -> Optional[List[str]]:
    """Список запущенных процессов, чьё имя совпадает с одним из names."""
    try:
        from core.executive import LocalWorldObserver
        observation = LocalWorldObserver().observe("processes", limit=4096)
    except Exception:
        return None
    if not observation.ok:
        return None
    wanted = {n.lower() for n in names if n}
    running: List[str] = []
    for item in observation.data.get("processes", []):
        pname = str(item.get("name") or "").lower()
        if not pname:
            continue
        for w in wanted:
            # notepad.exe == notepad / notepad.exe
            if pname == w or pname == f"{w}.exe" or w in pname:
                running.append(pname)
                break
    return running


def _app_candidate_names(result: ActionResult) -> List[str]:
    """Возможные имена процессов для запрошенного приложения."""
    names: List[str] = []
    app = (result.args or {}).get("name") or (result.args or {}).get("app")
    if isinstance(app, str) and app.strip():
        names.append(app.strip())
        try:
            from core.actions.app_control import _APP_PROCESS_NAMES, resolve_app
            key = app.strip().lower()
            names.extend(_APP_PROCESS_NAMES.get(key, []))
            # Built-in aliases already provide the canonical process names.
            # Avoid a synchronous PATH/``where.exe`` lookup on the verified
            # fast path; resolve arbitrary names only when no alias exists.
            if not _APP_PROCESS_NAMES.get(key):
                resolved = resolve_app(app)
                if isinstance(resolved, str) and resolved:
                    names.append(Path(resolved).name)
        except Exception:
            pass
    # Из output вида "Запустил notepad."
    text = _output_text(result)
    m = re.search(r"[Зз]апустил\s+([^\s.,]+)", text)
    if m:
        names.append(m.group(1))
    return [n for n in names if n]


def verify_process_running(result: ActionResult) -> VerificationResult:
    """open_app: процесс реально существует в системе (§14)."""
    if not result.ok:
        return VerificationResult(False, "process_running", result.error or "ok=False")

    app_name = str((result.args or {}).get("name") or "").casefold()
    launch_args = str((result.args or {}).get("args") or "")
    if app_name in {"explorer", "проводник"} and launch_args:
        match = re.search(r"/select,\s*\"?(.+?)\"?$", launch_args, re.IGNORECASE)
        requested = Path(match.group(1)).expanduser().resolve().parent if match else Path(launch_args.strip('"')).expanduser().resolve()
        deadline = time.time() + 4.0
        last_locations: List[str] = []
        while time.time() < deadline:
            try:
                import os
                import pythoncom
                import win32com.client
                from urllib.parse import unquote, urlparse

                pythoncom.CoInitialize()
                last_locations = []
                for window in win32com.client.Dispatch("Shell.Application").Windows():
                    location_url = str(getattr(window, "LocationURL", "") or "")
                    if not location_url.casefold().startswith("file:"):
                        continue
                    parsed = urlparse(location_url)
                    location = unquote(parsed.path).lstrip("/")
                    if parsed.netloc:
                        location = f"//{parsed.netloc}/{location}"
                    observed = Path(location).resolve()
                    last_locations.append(str(observed))
                    if os.path.normcase(str(observed)) == os.path.normcase(str(requested)):
                        return VerificationResult(
                            True, "explorer_location",
                            f"проводник физически открыт в {requested}", strict=True,
                        )
            except Exception as exc:
                last_locations = [f"{type(exc).__name__}: {exc}"]
            time.sleep(0.25)
        return VerificationResult(
            False, "explorer_location",
            f"целевая папка {requested} не наблюдалась; окна={last_locations[:3]}", strict=True,
        )

    # The launcher returns the concrete PID when it owns the process. A PID
    # probe is stronger and faster than waiting for a GUI name scan while a
    # Windows app paints its first frame.
    pid_match = _PID_RE.search(_output_text(result))
    if pid_match:
        try:
            import psutil
            pid = int(pid_match.group(1))
            if psutil.pid_exists(pid):
                return VerificationResult(True, "process_running", f"процесс запущен: pid={pid}")
        except (ImportError, ValueError, OSError):
            pass

    names = _app_candidate_names(result)
    if not names:
        return VerificationResult(False, "process_running",
                                  "имя процесса неизвестно — строгая проверка невозможна",
                                  strict=False)

    # Приложению нужно время на старт — короткий реальный поллинг (не «лимит мышления»).
    deadline = time.time() + 1.5
    observation_failed = False
    while time.time() < deadline:
        running = _process_matches(names)
        if running is None:
            observation_failed = True
            time.sleep(0.4)
            continue
        if running:
            return VerificationResult(True, "process_running",
                                      f"процесс запущен: {sorted(set(running))[:3]}")
        time.sleep(0.4)

    # Протокольные цели (ms-settings:) и UWP не дают совпадения по имени.
    text = _output_text(result)
    if ":" in text and "ms-settings" in text.lower():
        return VerificationResult(False, "shell_protocol",
                                  "запущена протокольная цель, процесса нет", strict=False)
    detail = (
        "наблюдение списка процессов недоступно"
        if observation_failed else
        f"процесс не найден среди запущенных: {names[:3]}"
    )
    return VerificationResult(False, "process_running", detail)


def verify_process_gone(result: ActionResult) -> VerificationResult:
    """close_app: процесс реально исчез (§14)."""
    if not result.ok:
        return VerificationResult(False, "process_gone", result.error or "ok=False")
    names = _app_candidate_names(result)
    if not names:
        return VerificationResult(False, "process_gone", "имя процесса неизвестно", strict=False)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        running = _process_matches(names)
        if running is None:
            return VerificationResult(False, "process_gone", "наблюдение списка процессов недоступно")
        if not running:
            return VerificationResult(True, "process_gone", f"процессы {names[:3]} завершены")
        time.sleep(0.4)
    return VerificationResult(False, "process_gone", f"процесс всё ещё запущен: {names[:3]}")


def verify_file_search(result: ActionResult) -> VerificationResult:
    """search_files: реально нашёл файл, а не «ничего не найдено» (§14)."""
    if not result.ok:
        return VerificationResult(False, "file_search", result.error or "ok=False")
    text = _output_text(result).lower()
    not_found = any(k in text for k in (
        "не найден", "не найдено", "ничего не найден", "ничего не",
        "no results", "not found", "0 file", "файлы не найд",
    ))
    if not_found:
        return VerificationResult(False, "file_search", "файл(ы) не найдены запросом")
    candidates: List[Path] = []
    for line in _output_text(result).splitlines():
        stripped = line.strip()
        if stripped.startswith("FILE "):
            candidates.append(Path(stripped[5:].split(" | ", 1)[0]).expanduser())
    existing = [path for path in candidates if path.is_file()]
    if candidates and not existing:
        return VerificationResult(False, "file_search", "пути из результата не существуют")
    if existing:
        return VerificationResult(
            True, "file_search",
            f"фактически существуют: {[str(path) for path in existing[:3]]}",
        )
    return VerificationResult(False, "file_search", "результат не содержит проверяемых путей")


def verify_non_empty_output(result: ActionResult) -> VerificationResult:
    """Инструмент обязан вернуть непустой осмысленный контент."""
    if not result.ok:
        return VerificationResult(False, "non_empty_output", result.error or "ok=False")
    text = _output_text(result).strip()
    if len(text) < 3:
        return VerificationResult(False, "non_empty_output",
                                  f"output слишком короткий ({len(text)} символов)")
    return VerificationResult(True, "non_empty_output", f"получено {len(text)} символов")


def verify_page_loaded(result: ActionResult) -> VerificationResult:
    """web_fetch: страница реально загрузилась и в ней есть текст."""
    if not result.ok:
        return VerificationResult(False, "page_loaded", result.error or "ok=False")
    text = _output_text(result)
    # web_fetch возвращает "Содержимое <url>:\n\n<текст>"
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    if len(body.strip()) < 50:
        return VerificationResult(False, "page_loaded",
                                  f"страница пуста или почти пуста ({len(body.strip())} символов)")
    return VerificationResult(True, "page_loaded", f"загружено {len(body)} символов текста")


def verify_search_results(result: ActionResult) -> VerificationResult:
    """web_search: есть хотя бы один результат."""
    if not result.ok:
        return VerificationResult(False, "search_results", result.error or "ok=False")
    text = _output_text(result).strip()
    if not text or "не найдено" in text.lower() or "ничего не найдено" in text.lower():
        return VerificationResult(False, "search_results", "поиск не дал результатов")
    return VerificationResult(True, "search_results", f"результаты получены ({len(text)} символов)")


def verify_reminder_registered(result: ActionResult) -> VerificationResult:
    """add_reminder: напоминание реально существует в менеджере."""
    if not result.ok:
        return VerificationResult(False, "reminder_registered", result.error or "ok=False")
    output = result.output if isinstance(result.output, Mapping) else {}
    if output.get("durable") is True:
        try:
            from core.security.atomic import load_json
            path = Path(str(output.get("persistence_path") or ""))
            stored = load_json(path, default={}) if path.is_file() else {}
            verified = bool(
                stored.get("task_id") == output.get("mission_id")
                and stored.get("status") == "waiting"
                and isinstance(stored.get("trigger"), Mapping)
                and stored["trigger"].get("trigger_id") == output.get("trigger_id")
                and stored.get("metadata", {}).get("durable") is True
            )
            return VerificationResult(
                verified, "durable_reminder_record",
                "durable waiting mission independently reloaded from disk"
                if verified else "durable mission record is missing or inconsistent",
            )
        except Exception as exc:
            return VerificationResult(False, "durable_reminder_record", f"persistence check failed: {exc}")
    try:
        from core.actions.reminders import get_default_manager
        reminders = get_default_manager().list_reminders()
        if reminders:
            return VerificationResult(True, "reminder_registered",
                                      f"активных напоминаний: {len(reminders)}")
        return VerificationResult(False, "reminder_registered", "список напоминаний пуст")
    except Exception as exc:
        return VerificationResult(False, "reminder_registered", f"проверка не удалась: {exc}")


def verify_system_metrics(result: ActionResult) -> VerificationResult:
    """system_status: в ответе реально есть метрики (CPU/RAM)."""
    if not result.ok:
        return VerificationResult(False, "system_metrics", result.error or "ok=False")
    output = result.output if isinstance(result.output, Mapping) else {}
    if output:
        observed = (
            output.get("fact_type") == "observed"
            and output.get("freshness") == "fresh"
            and bool(output.get("observed_at"))
            and bool(output.get("source"))
            and "cpu_percent" in output
            and isinstance(output.get("ram"), Mapping)
        )
        return VerificationResult(
            observed,
            "system_metrics_observation",
            "fresh OS metrics with provenance" if observed else "structured OS evidence is incomplete",
        )
    text = _output_text(result).lower()
    if any(k in text for k in ("cpu", "процессор", "ram", "память", "диск", "%")):
        return VerificationResult(False, "system_metrics", "legacy text has no freshness/provenance", strict=False)
    return VerificationResult(False, "system_metrics", "метрик в ответе нет")


def verify_current_time(result: ActionResult) -> VerificationResult:
    """current_time: локальный ответ содержит время и дату."""
    if not result.ok:
        return VerificationResult(False, "current_time", result.error or "ok=False")
    text = _output_text(result)
    if re.search(r"\b\d{2}:\d{2}:\d{2}\b", text) and re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        return VerificationResult(True, "current_time", "локальные часы и дата присутствуют")
    return VerificationResult(False, "current_time", "формат времени не подтверждён")


def verify_play_music(result: ActionResult) -> VerificationResult:
    """play_music: an active audio session exists after the launch request."""
    text = _output_text(result)
    output = result.output if isinstance(result.output, Mapping) else {}
    stage = str(output.get("stage") or "").casefold()
    args = result.args or {}
    source = str(args.get("source") or "auto").casefold()
    query = str(args.get("query") or "").strip()
    if result.ok and (
        stage in {"search_opened", "service_opened"}
        or "поиск музыки" in text.casefold()
        or (source == "spotify" and bool(query))
    ):
        return VerificationResult(
            False,
            "media_playback",
            "открыта поисковая страница; воспроизведение не подтверждено",
        )
    if not result.ok:
        return VerificationResult(False, "media_playback", result.error or "медиаточка не открыта")
    deadline = time.time() + 2.0
    active: List[str] = []
    while time.time() < deadline and not active:
        active = _active_audio_sessions()
        if not active:
            time.sleep(0.2)
    if active:
        return VerificationResult(
            True,
            "active_audio_session",
            f"активное воспроизведение наблюдалось: {active[:3]}",
        )
    return VerificationResult(
        False,
        "media_playback",
        "медиаповерхность открыта, но активное воспроизведение не наблюдалось",
    )


def verify_public_data(result: ActionResult) -> VerificationResult:
    """Fresh public data must carry a non-empty structured observation."""
    if not result.ok or not isinstance(result.output, Mapping):
        return VerificationResult(
            False, "fresh_public_data",
            result.error or "структурированные данные отсутствуют",
        )
    observed = bool(
        result.output.get("rates")
        or result.output.get("items")
        or result.output.get("text")
    )
    return VerificationResult(
        observed,
        "fresh_public_data",
        "получены свежие структурированные данные" if observed else "источник вернул пустые данные",
    )


def _active_audio_sessions() -> List[str]:
    """Observe active Windows audio sessions independently of the launcher."""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return []
    active: List[str] = []
    try:
        for session in AudioUtilities.GetAllSessions():
            if int(getattr(session, "State", 0) or 0) != 1:
                continue
            process = getattr(session, "Process", None)
            name = process.name() if process is not None else "system-audio"
            if name and name not in active:
                active.append(str(name))
    except Exception as exc:
        log.debug("Audio session observation failed: %s", exc)
    return active


def verify_computer_action(result: ActionResult) -> VerificationResult:
    """Computer tools: physical backend plus a post-action observation."""
    if not result.ok:
        return VerificationResult(False, "computer_observation", result.error or "ok=False")
    output = result.output if isinstance(result.output, Mapping) else {}
    if not output.get("physical"):
        return VerificationResult(False, "computer_physical", "physical backend was not active")
    if result.tool == "computer_screenshot":
        path = Path(str(output.get("path") or ""))
        if path.is_file() and path.stat().st_size > 100:
            return VerificationResult(True, "physical_screenshot", f"PNG captured: {path}")
        return VerificationResult(False, "physical_screenshot", "PNG file is missing or empty")
    args = result.args or {}
    action = str(args.get("action") or "")
    backend = output.get("backend_result") if isinstance(output.get("backend_result"), Mapping) else {}
    if action == "move" and backend.get("observed_x") == backend.get("px") \
            and backend.get("observed_y") == backend.get("py"):
        return VerificationResult(True, "cursor_position", "cursor reached requested coordinates")
    if output.get("observed"):
        return VerificationResult(True, "computer_observation", "active window or screen changed after physical input")
    return VerificationResult(False, "computer_observation", "physical input was sent but the requested outcome was not observed")


def verify_browser_bridge(result: ActionResult) -> VerificationResult:
    """BrowserBridge: verify navigation or semantic post-action observation."""
    if not result.ok:
        return VerificationResult(False, "browser_observation", result.error or "ok=False")
    output = result.output if isinstance(result.output, Mapping) else {}
    action = str((result.args or {}).get("action") or "")
    if str(output.get("evidence_scope") or "") != "user_visible":
        return VerificationResult(
            False,
            "browser_visibility",
            "browser observation is not proven user-visible",
        )
    if action in {"open", "navigate", "observe"}:
        url = str(output.get("url") or "")
        dom_hash = str(output.get("dom_hash") or "")
        if url.startswith(("http://", "https://")) and dom_hash:
            return VerificationResult(True, "browser_navigation", f"observed {url}")
        return VerificationResult(False, "browser_navigation", "URL/DOM observation is missing")
    if action in {"click", "type", "press", "download"}:
        verification = output.get("verification") if isinstance(output.get("verification"), Mapping) else {}
        if output.get("action_taken") and verification.get("ok"):
            return VerificationResult(True, str(verification.get("method") or "browser_action"),
                                      str(verification.get("detail") or "post-action state observed"))
        return VerificationResult(False, "browser_action", str(output.get("error") or "action outcome was not observed"))
    if action in {"read", "extract", "inspect_dom", "find", "wait", "close"} and output:
        return VerificationResult(True, "browser_observation", "browser result is non-empty")
    return VerificationResult(False, "browser_observation", "browser result is empty")


def verify_internal_browser(result: ActionResult) -> VerificationResult:
    """Headless automation can support internal work, never a visible goal."""
    return VerificationResult(
        False,
        "internal_browser_observation",
        result.error or "headless browser state is internal evidence only",
        strict=True,
    )


def default_verify(result: ActionResult) -> VerificationResult:
    """Fallback records provider acknowledgement without granting completion."""
    if result.ok:
        log.debug("Инструмент '%s' без спец. verifier — completion не подтверждён", result.tool)
        return VerificationResult(False, "trust_ok",
                                  "специализированная проверка недоступна", strict=False)
    return VerificationResult(False, "trust_ok", result.error or "ok=False", strict=False)


def verify_action_result(result: ActionResult) -> VerificationResult:
    """Главная точка входа: фактическая проверка результата инструмента.

    Ошибка verifier-а — это ошибка проверки, а не успешное действие. Она
    возвращается с точной причиной и строгим ``verified=False``.
    """
    tool = getattr(result, "tool", "") or ""
    verifier = _VERIFIERS.get(tool)
    if verifier is not None:
        try:
            return verifier(result)
        except Exception as exc:
            log.warning("Verifier для '%s' упал: %s", tool, exc)
            return VerificationResult(
                False,
                "verifier_error",
                f"{type(exc).__name__}: {exc}",
                strict=True,
            )
    return default_verify(result)


# --------------------------------------------------------------------------- #
#  Регистрация проверок для реальных инструментов проекта
# --------------------------------------------------------------------------- #

register_verifier("write_file", verify_file_exists)
register_verifier("read_file", verify_non_empty_output)
register_verifier("list_files", verify_non_empty_output)
register_verifier("search_files", verify_file_search)
register_verifier("open_app", verify_process_running)
register_verifier("close_app", verify_process_gone)
register_verifier("web_fetch", verify_page_loaded)
register_verifier("web_search", verify_search_results)
register_verifier("system_status", verify_system_metrics)
register_verifier("current_time", verify_current_time)
register_verifier("play_music", verify_play_music)
register_verifier("weather", verify_non_empty_output)
register_verifier("public_data", verify_public_data)
register_verifier("add_reminder", verify_reminder_registered)
register_verifier("list_reminders", verify_non_empty_output)
register_verifier("computer_mouse", verify_computer_action)
register_verifier("computer_keyboard", verify_computer_action)
register_verifier("computer_screenshot", verify_computer_action)
register_verifier("browser_bridge", verify_browser_bridge)
register_verifier("browser_automation", verify_internal_browser)
