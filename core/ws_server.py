"""WebSocket-мост J.A.R.V.I.S. (P5 §5.2 / §5.4 / §5.7).

Поднимает асинхронный ``websockets`` сервер РЯДОМ с существующим
консольным REPL (main.py). Сервер не заменяет логику backend — он
переиспользует тот же ``Orchestrator`` и тот же единый путь
``Orchestrator.handle_input`` (P5 §5.7: оба входа — консоль и WS —
идут через один и тот же роутер ``Agent.execute`` -> ``ModelRouter``).

Протокол (JSON, одна строка на сообщение):

  Клиент -> Сервер:
    {"type": "command",  "text": "<текст команды>"}
    {"type": "confirm",  "confirmation_id": "<id>", "approve": true|false,
                         "scope": "once"|"session"|"permanent"}
    {"type": "screen_capture"}
    {"type": "interrupt"}
    {"type": "ping"}

  ``scope`` относится только к подтверждениям, которые выдают полномочие
  (S3: снимок экрана). Само разрешение клиент запросить не может: у
  ``screen_capture`` нет аргументов, решение принимает сервер по гранту
  ``core.authority``.

  Сервер -> Клиент (broadcast на всех подключённых):
    {"type": "state",    "state": "thinking"|"executing"|"streaming"|"idle"|"error"|"cloud"|"listening"}
    {"type": "event",    "event": { ... ActivityEvent-совместимый объект ... }}
    {"type": "confirmation_required", "confirmation_id": "<id>", "prompt": "...", "tool": "...", "risk": {...}, "scopes": [...]}
    {"type": "vitals",   "vitals": {...}}
    {"type": "pong"}
    {"type": "error",    "message": "..."}

События миссий (TaskRuntime) транслируются в ``event``/``state``
сообщения, понятные фронтенду из ``jarvis/src/integrations/backend.ts``
(mapTransportEvent ожидает типы с префиксом ``state:`` / ``event:``).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
import threading
import time
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse
from uuid import uuid4

from config import load_config
from config.settings import Settings
from core.authority import (
    SCREEN_SCOPES,
    screen_capture_proposal,
    screen_capture_request,
)
from core.orchestrator import Orchestrator
from core.task_runtime import (
    EVENT_ACKNOWLEDGED,
    EVENT_CONFIRMATION_REQUIRED,
    EVENT_STREAM_CHUNK,
    EVENT_STREAM_END,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    TaskEvent,
)
from core.utils.logger import get_logger
from core.llm.backend import strip_reasoning_blocks

try:
    import websockets  # type: ignore
    from websockets.server import serve
    _HAS_WS = True
except Exception:  # pragma: no cover - optional dep
    _HAS_WS = False

__all__ = ["JarvisWSServer", "run_server"]

log = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8771

# ДЫРА 5 FIX: любое сообщение об ошибке, уходящее клиенту по WS, проходит
# через этот фильтр. Pydantic-валидация вставляет сырые input_value в текст
# исключения, поэтому значения (в т.ч. API-ключи) вырезаются до redact_secrets.
_INPUT_VALUE_RE = re.compile(
    r"input_value=(?:'[^']*'|\"[^\"]*\"|[^,\]\n]+)", re.IGNORECASE,
)


def _safe_error_text(exc: BaseException) -> str:
    """Человекочитаемый текст ошибки без секретов — безопасен для WS-клиента."""
    text = str(exc)
    try:
        text = _INPUT_VALUE_RE.sub("input_value=<omitted>", text)
        from core.redact import redact_secrets
        text = str(redact_secrets(text))
    except Exception as filter_exc:  # pragma: no cover — фильтр не должен ломать emit
        log.debug("Secret filter не сработал на тексте ошибки: %s", filter_exc)
    return text

_DEFAULT_ALLOWED_ORIGINS = frozenset({
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
})

#: S2: сколько ждать ``{"type":"auth"}`` от клиента. До этого принимается
#: только ``ping``, поэтому окно короткое.
_AUTH_TIMEOUT_SEC = 5.0

# Маппинг статусов миссии -> entity state фронтенда
_STATUS_TO_STATE = {
    "queued": "thinking",
    "acknowledging": "thinking",
    "analyzing": "thinking",
    "planning": "thinking",
    "executing": "executing",
    "verifying": "executing",
    "repairing": "executing",
    "completed": "idle",
    "paused": "idle",
    "cancelled": "idle",
    "failed": "error",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


class JarvisWSServer:
    """Мост Orchestrator <-> WebSocket-клиенты."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        auth_token: str | None = None,
        allowed_origins: set[str] | None = None,
        max_messages_per_window: int = 60,
        rate_window_sec: float = 10.0,
    ) -> None:
        self._orch = orchestrator
        # Settings — единый runtime-объект, переданный Orchestrator. Его
        # обновление через WS сразу доступно CouncilRouter/Agent без
        # параллельного формата или перезапуска процесса.
        orchestrator_settings = getattr(orchestrator, "_settings", None)
        self._settings: Settings = (
            orchestrator_settings if orchestrator_settings is not None else Settings()
        )
        self._host = host
        self._port = port
        self._auth_token = auth_token
        # S2: дефолт — явный список, а не пустое множество. Пустое множество
        # раньше означало «Origin не проверяем» (fail-open); теперь оно
        # означает «не пускать никого», поэтому подставить его по умолчанию
        # нельзя — сервер стал бы неработоспособным вместо небезопасного.
        self._allowed_origins = set(
            allowed_origins if allowed_origins is not None else _DEFAULT_ALLOWED_ORIGINS
        )
        if not auth_token:
            log.warning(
                "JarvisWSServer создан без auth_token: мост принимает команды от "
                "любого локального процесса. Продуктовый вход (run_server) такой "
                "конфигурации не допускает."
            )
        self._max_messages_per_window = max(1, int(max_messages_per_window))
        self._rate_window_sec = max(1.0, float(rate_window_sec))
        self._authorized_clients: Set[Any] = set()
        self._message_times: dict[int, list[float]] = {}
        self._clients: Set[Any] = set()
        # S3: снимок экрана ждёт решения пользователя, а не флага в сообщении.
        # confirmation_id -> {"client": id(ws)}; запись живёт до ответа
        # `confirm` и снимается в любом исходе (разрешение или отказ).
        self._pending_screen: dict[str, dict[str, Any]] = {}
        # id(ws) -> grant_id: гранты со scope=session привязаны к соединению
        # и отзываются при отключении, иначе «на сессию» означало бы «до
        # перезапуска процесса».
        self._session_screen_grants: dict[int, set[str]] = {}
        self._lock = threading.RLock()
        # task_id, для которых уже отправлен event:jarvis:start (чтобы не
        # дублировать start на каждый токен в streaming-пути).
        self._streaming_started: Set[str] = set()
        # БАГ 2 FIX: task_id миссий, для которых _output_callback уже
        # отправил финальный пузырь (start+end). _on_task_event проверяет
        # этот set чтобы не создавать дублирующий пузырь из EVENT_TASK_COMPLETED.
        self._output_sent: Set[str] = set()
        self._running = False
        self._stop_event = threading.Event()
        self._ws_server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._unsub: Optional[Any] = None
        # Sprint 1: per-request контекст (thread-local). Каждый WS-запрос
        # обрабатывается в своём потоке executor'а, поэтому параллельные
        # запросы имеют независимые correlation ID и не перемешиваются.
        self._tls = threading.local()
        # Sprint 5: приветствие отправляется ОДИН раз за сессию сервера —
        # при подключении первого клиента (не при каждом открытии окна).
        self._greeted = False
        self._system_monitor = None
        self._stt_engine = None
        self._voice_addressed_until = 0.0
        try:
            from core.voice.stt import STTEngine
            candidate = STTEngine(self._settings)
            if candidate.is_available(): self._stt_engine = candidate
        except Exception as exc:
            log.debug("STT startup probe failed: %s", exc)
        # Перехватываем output_callback, чтобы синхронный fast-path
        # ответ тоже доходил до фронта (handle_input сам его печатает).
        self._orig_output = orchestrator._output_callback

        def _emit_jarvis_event(etype: str, rid: str, payload: Dict[str, Any]) -> None:
            self._emit({
                "type": "event",
                "event": {
                    "type": etype,
                    "payload": {"id": rid, "kind": "jarvis", **payload},
                    "timestamp": _now_ms(),
                },
            })

        def _cb(text: str) -> None:
            # Оркестратор уже отправил typed AssistantOutput в TTS. Callback
            # отвечает только за UI transport — повторная raw озвучка запрещена.
            text = strip_reasoning_blocks(str(text or "")).strip()
            try:
                self._orig_output(text)
            except Exception:
                pass
            # Транслируем в события J.A.R.V.I.S.
            # ВАЖНО (P5 §5.4/frontend): фронт рендерит пузырь ответа ТОЛЬКО после
            # event:jarvis:start (useBackendBridge создаёт пузырь по start, а
            # token/end лишь обновляют его).
            #
            # Sprint 1 STEP 1: correlation ID создаётся ОДИН РАЗ на весь
            # логический ответ (uuid, без timestamp-гонки). Если этот запрос
            # стримился (пузырь с rid уже открыт) — закрываем ЕГО тем же rid.
            tls = self._tls
            rid = getattr(tls, "cid", None)
            if rid and getattr(tls, "started", False) and not getattr(tls, "ended", False):
                tls.ended = True
                _emit_jarvis_event("event:jarvis:end", rid, {"content": text, "model": self._brain_model_label()})
                self._emit({"type": "state", "state": "idle"})
                return
            # Стримленная миссия этого потока: финальный текст закрывает
            # её пузырь (тот же task_id), не создавая второй ответ.
            consume = getattr(self._orch, "consume_streamed_mission", None)
            mission_rid = consume() if callable(consume) else None
            if mission_rid:
                self._streaming_started.discard(mission_rid)
                # БАГ 2 FIX: помечаем что пузырь уже закрыт — _on_task_event
                # не должен создавать второй при получении EVENT_TASK_COMPLETED.
                self._output_sent.add(mission_rid)
                _emit_jarvis_event("event:jarvis:end", mission_rid,
                                   {"content": text, "model": self._brain_model_label()})
                self._emit({"type": "state", "state": "idle"})
                return
            response_id = uuid4().hex
            # БАГ 2 FIX: для нестримленных миссий тоже помечаем output_sent
            # чтобы _on_task_event не создавал дублирующий пузырь.
            # response_id здесь не task_id, но _on_task_event проверяет
            # _streaming_started по task_id — для нестримленных миссий
            # task_id не в _streaming_started, поэтому дубль возникает.
            # Решение: _on_task_event проверяет _output_sent по task_id.
            # Для нестримленных миссий task_id передаётся через mission.metadata.
            # Здесь мы не знаем task_id — используем response_id как маркер.
            _emit_jarvis_event("event:jarvis:start", response_id, {"content": ""})
            _emit_jarvis_event("event:jarvis:end", response_id, {"content": text, "model": self._brain_model_label()})
            self._emit({"type": "state", "state": "idle"})

        orchestrator._output_callback = _cb

        # Proactor is an existing system-trigger source.  Preserve its output
        # callback (notifications/TTS) and mirror it as a distinct UI event;
        # it never becomes a user command or an action request.
        proactor = getattr(orchestrator, "proactor", None)
        if proactor is not None and hasattr(proactor, "_output"):
            original_proactive = proactor._output

            def _proactive_cb(text: str) -> None:
                original_proactive(text)
                self._emit({
                    "type": "event",
                    "event": {
                        "type": "event:system_initiated",
                        "payload": {"text": text},
                        "timestamp": _now_ms(),
                    },
                })

            proactor._output = _proactive_cb
        try:
            from core.triggers import SystemMonitor, SystemTriggerEngine
            configured = getattr(self._settings, "system_triggers", []) or []
            self._system_monitor = SystemMonitor(SystemTriggerEngine(configured, emit=lambda event, text: self._emit({"type": "event", "event": {"type": "event:system_initiated", "payload": {"text": text}, "timestamp": _now_ms()}}))) if configured else None
        except Exception as exc:
            log.warning("System triggers disabled: %s", exc)

    def _brain_model_label(self) -> str:
        if bool(getattr(self._settings, "deepseek_brain_mode", False)):
            return str(
                getattr(self._settings, "deepseek_model", "")
                or "deepseek-ai/DeepSeek-V4-Flash-0731"
            )
        return "local"

    def _runtime_status_payload(self) -> Dict[str, Any]:
        """Return product readiness independently from optional local warmup.

        Шаг 6: ready только когда semantic-роутер прогрет (route() отвечает
        за <= 40 мс); провайдер отражается отдельным полем, а не выводится
        из включённого режима.
        """
        try:
            diagnostics = self._orch.runtime_diagnostics()
        except Exception:
            diagnostics = {}
        warmup = diagnostics.get("warmup") if isinstance(diagnostics, dict) else {}
        if not isinstance(warmup, dict):
            warmup = {}
        router_info = (
            diagnostics.get("router") if isinstance(diagnostics, dict) else {}
        )
        if not isinstance(router_info, dict):
            router_info = {}
        router_ready = bool(router_info.get("ready"))
        provider = (
            str(diagnostics.get("provider") or "unavailable")
            if isinstance(diagnostics, dict) else "unavailable"
        )
        if bool(getattr(self._settings, "deepseek_brain_mode", False)):
            ready = router_ready
            return {
                "type": "runtime_status",
                "state": "ready" if ready else "starting",
                "ready": ready,
                "router": {"ready": router_ready, **router_info},
                "provider": provider,
                # C3: правда о провайдере — источник ключа, живость, цепь.
                "provider_effective": (
                    diagnostics.get("provider_effective")
                    if isinstance(diagnostics, dict) else None
                ),
                "diagnostics": {**warmup, "brain": "deepseek"},
            }
        warmup_state = str(warmup.get("state", "") or "").casefold()
        state = (
            "ready" if diagnostics.get("warmup_ready") and warmup_state == "ready"
            and router_ready else
            "unavailable" if warmup_state == "unavailable" else
            "loading_model" if getattr(self._orch, "_warmup_thread", None) is not None
            else "starting"
        )
        return {
            "type": "runtime_status", "state": state, "ready": state == "ready",
            "router": {"ready": router_ready, **router_info},
            "provider": provider,
            "provider_effective": (
                diagnostics.get("provider_effective")
                if isinstance(diagnostics, dict) else None
            ),
            "diagnostics": warmup,
        }

    # ----------------------------------------------------------------- #
    #  Sprint 5 — ALWAYS-ON TTS + приветствие
    # ----------------------------------------------------------------- #

    def _tts_queue(self):
        """TTSQueue оркестратора (None, если недоступен)."""
        return getattr(self._orch, "_tts_queue", None)

    def _speak(self, output) -> None:
        """Typed greeting/system path; arbitrary WS messages are never spoken."""
        try:
            from core.voice.output import AssistantOutput
            if not isinstance(output, AssistantOutput):
                raise TypeError("WS speech boundary accepts AssistantOutput only")
            voice = getattr(self._settings, "voice", None)
            if not getattr(voice, "tts_enabled", True):
                return
            if not getattr(voice, "tts_always_on", True):
                return
            tts = self._tts_queue()
            if tts is None:
                return
            tts.add_output(output)
        except TypeError as exc:
            # S5: неверный тип на границе речи — ошибка вызывающего, а не
            # необязательность TTS. Раньше она глоталась на debug, и
            # ambient-инициативы молча не озвучивались.
            log.error("TTS-озвучка отклонена: %s", exc)
        except Exception as exc:  # noqa: BLE001 — TTS не ломает ответы
            log.debug("TTS-озвучка пропущена: %s", exc)

    def _tts_interrupt(self) -> None:
        """Barge-in (Sprint 5 STEP 5.3): прерывает текущую речь и очередь."""
        try:
            tts = self._tts_queue()
            if tts is not None:
                tts.interrupt()
        except Exception as exc:  # noqa: BLE001
            log.debug("TTS-interrupt пропущен: %s", exc)

    def _send_startup_greeting(self) -> None:
        """Приветствие при старте сессии (Sprint 5 STEP 3).

        Условия: launcher.greeting_enabled, ещё не отправляли, профиль
        читается для персонального обращения. Идёт через обычный WS
        pipeline (event:jarvis:start/end) и озвучивается always-on TTS.
        """
        launcher = getattr(self._settings, "launcher", None)
        if not getattr(launcher, "greeting_enabled", True):
            return
        try:
            from core.voice.greeting import build_startup_greeting
            return_context = None
            try:
                living = getattr(self._orch, "_living", None)
                if living is not None:
                    return_context = living.context.return_context(min_confidence=0.75)
            except Exception as exc:  # context is optional; ordinary greeting remains
                log.debug("Контекст прошлого сеанса недоступен: %s", exc)
            text = build_startup_greeting(self._settings, return_context=return_context)
        except Exception as exc:  # noqa: BLE001
            log.warning("Приветствие не сформировано: %s", exc)
            return
        rid = uuid4().hex
        log.info("Приветствие сессии: %s", text)
        self._emit({
            "type": "event",
            "event": {
                "type": "event:jarvis:start",
                "payload": {"id": rid, "kind": "jarvis", "content": ""},
                "timestamp": _now_ms(),
            },
        })
        self._emit({
            "type": "event",
            "event": {
                "type": "event:jarvis:end",
                "payload": {"id": rid, "kind": "jarvis",
                            "content": text, "model": self._brain_model_label()},
                "timestamp": _now_ms(),
            },
        })
        from core.voice.output import AssistantOutput
        self._speak(AssistantOutput.natural(text))

    # ----------------------------------------------------------------- #
    #  Управление подключениями
    # ----------------------------------------------------------------- #
    async def _handler(self, ws):
        peer = getattr(ws, "remote_address", "?")
        readiness_task = None
        log.info("WS клиент подключён: %s", peer)
        if not self._origin_allowed(ws):
            await ws.close(code=1008, reason="origin not allowed")
            return
        if self._auth_token and not await self._authenticate(ws):
            await ws.close(code=1008, reason="authentication required")
            return
        with self._lock:
            self._clients.add(ws)
            if self._auth_token:
                self._authorized_clients.add(ws)
            # Sprint 5 STEP 3: приветствие — ОДИН раз за сессию сервера,
            # при подключении первого клиента (не при каждом открытии окна).
            first_client = len(self._clients) == 1 and not self._greeted
            if first_client:
                self._greeted = True
        try:
            await ws.send(json.dumps({"type": "state", "state": "idle"}))
            # Readiness is a first-class protocol event.  It follows the
            # legacy idle envelope so existing clients keep their first-frame
            # contract while newer clients render a real startup state.
            runtime_payload = self._runtime_status_payload()
            runtime_state = str(runtime_payload["state"])
            await ws.send(json.dumps(runtime_payload))
            if runtime_state == "loading_model":
                async def _publish_readiness() -> None:
                    wait_ready = getattr(self._orch, "wait_for_runtime_ready", None)
                    if not callable(wait_ready):
                        return
                    await asyncio.to_thread(wait_ready)
                    try:
                        updated = self._orch.runtime_diagnostics()
                    except Exception as exc:
                        log.warning("Не удалось обновить readiness WS: %s", exc)
                        updated = {}
                    updated_warmup = updated.get("warmup") if isinstance(updated, dict) else {}
                    if not isinstance(updated_warmup, dict):
                        updated_warmup = {}
                    updated_state = (
                        "ready" if updated.get("warmup_ready") and updated_warmup.get("state") == "ready" else
                        "unavailable" if updated_warmup.get("state") == "unavailable" else
                        "starting"
                    )
                    self._emit({
                        "type": "runtime_status",
                        "state": updated_state,
                        "ready": updated_state == "ready",
                        "diagnostics": updated_warmup,
                    })

                readiness_task = asyncio.create_task(_publish_readiness())
            try:
                from core.memory.profile import load_profile
                has_name = bool((load_profile(self._settings).get("name") or "").strip())
            except Exception:
                has_name = False
            await ws.send(json.dumps({"type": "profile", "has_name": has_name}))
            # ДЫРА 3 FIX: если стримы начались, когда клиент ещё не был
            # подключён (переподключение или второй клиент), новый клиент
            # получает start-конверты для активных стримов — иначе его
            # пузырь зависнет открытым до конца сессии.
            for envelope in self._stream_snapshot_envelopes():
                await ws.send(json.dumps(envelope))
            if first_client and not bool(getattr(self._settings, "deepseek_brain_mode", False)):
                # Пауза, чтобы фронт успел подписаться на события.
                await asyncio.sleep(0.5)
                self._send_startup_greeting()
            async for raw in ws:
                await self._on_message(ws, raw)
        except websockets.exceptions.ConnectionClosed:  # type: ignore
            pass
        except Exception as exc:  # pragma: no cover - network edge
            log.error("WS соединение упало: %s", exc)
        finally:
            if readiness_task is not None:
                readiness_task.cancel()
            # S3: «на сессию» означает на это соединение, поэтому гранты
            # закрываются до того, как клиент исчезнет из учёта.
            self._revoke_session_screen_grants(ws)
            with self._lock:
                self._clients.discard(ws)
                self._authorized_clients.discard(ws)
                self._message_times.pop(id(ws), None)
            log.info("WS клиент отключён: %s", peer)

    def _stream_snapshot_envelopes(self) -> list:
        """ДЫРА 3 FIX: start-конверты для всех активных стримов.

        Вызывается при подключении нового WS-клиента: задача может стримить
        с момента до подключения (переподключение UI), и без этого снимка
        фронт не откроет пузырь и не закроет его по end.
        """
        with self._lock:
            active = list(self._streaming_started)
        return [
            {
                "type": "event",
                "event": {
                    "type": "event:jarvis:start",
                    "payload": {"id": rid, "kind": "jarvis", "content": ""},
                    "timestamp": _now_ms(),
                },
            }
            for rid in active
        ]

    def _origin_allowed(self, ws: Any) -> bool:
        """S2: fail-closed. Пустой список — не «всё разрешено», а «ничего».

        Было наоборот: ``if not self._allowed_origins: return True``. Любая
        конфигурация, где список origins не задан (в том числе прямое
        конструирование сервера в коде), открывала мост для страницы из
        браузера. Единственный источник дефолта — ``_DEFAULT_ALLOWED_ORIGINS``,
        и он подставляется в конструкторе явно.
        """
        if not self._allowed_origins:
            log.error("WS: список разрешённых Origin пуст — соединение отклонено")
            return False
        headers = getattr(ws, "request_headers", {}) or {}
        origin = headers.get("Origin") if hasattr(headers, "get") else None
        return bool(origin and origin in self._allowed_origins)

    async def _authenticate(self, ws: Any) -> bool:
        """S2: аутентификация до любой полезной команды.

        Принимается ровно два способа: заголовок ``Authorization: Bearer`` и
        первое сообщение ``{"type":"auth","token":…}``. Путь ``?token=`` УДАЛЁН
        (S2): query string попадает в access-логи, историю и телеметрию прокси.
        До успешной аутентификации разрешён только ``ping`` — всё остальное
        закрывает соединение.
        """
        expected = self._auth_token or ""
        headers = getattr(ws, "request_headers", {}) or {}
        auth = headers.get("Authorization", "") if hasattr(headers, "get") else ""
        if hmac.compare_digest(str(auth or ""), f"Bearer {expected}"):
            return True
        await ws.send(json.dumps({"type": "auth_required"}))
        deadline = time.monotonic() + _AUTH_TIMEOUT_SEC
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                message = json.loads(raw)
            except Exception:
                return False
            if not isinstance(message, dict):
                return False
            mtype = str(message.get("type") or "").lower()
            if mtype == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            if mtype != "auth":
                await ws.send(json.dumps(
                    {"type": "error", "message": "authentication required"}))
                return False
            token = message.get("token")
            return bool(isinstance(token, str)
                        and hmac.compare_digest(token, expected))

    def _rate_allowed(self, ws: Any) -> bool:
        now = time.monotonic()
        key = id(ws)
        with self._lock:
            values = [stamp for stamp in self._message_times.get(key, []) if now - stamp < self._rate_window_sec]
            if len(values) >= self._max_messages_per_window:
                self._message_times[key] = values
                return False
            values.append(now)
            self._message_times[key] = values
        return True

    def _cloud_settings_payload(self) -> Dict[str, Any]:
        """Безопасное представление cloud-профиля для UI.

        API-ключ намеренно НИКОГДА не входит в WebSocket-ответ: клиенту
        доступны лишь флаг наличия и маска последних четырёх символов.
        """
        provider = self._settings.tier_providers.get("analyst")
        key = self._settings.api_keys.get(provider) or ""
        suffix = key[-4:] if key else ""
        return {
            "provider": provider,
            "base_url": self._settings.api_endpoints.get(provider) or "",
            "model": self._settings.model_tiers.get("analyst") or "",
            "has_api_key": bool(key),
            "api_key_masked": f"••••{suffix}" if key else "",
        }

    # ----------------------------------------------------------------- #
    #  Sprint 5 — Launcher API (autostart / hotkey / greeting)
    # ----------------------------------------------------------------- #

    def _launcher_payload(self) -> Dict[str, Any]:
        """Текущая конфигурация launcher для UI."""
        launcher = getattr(self._settings, "launcher", None)
        if launcher is None:
            return {}
        return {
            "autostart": getattr(launcher, "autostart", False),
            "hotkey": getattr(launcher, "hotkey", "Ctrl+Space"),
            "backend_command": getattr(launcher, "backend_command", []),
            "backend_workdir": getattr(launcher, "backend_workdir", ""),
            "greeting_enabled": getattr(launcher, "greeting_enabled", True),
        }

    def _update_launcher(self, patch: Any) -> Dict[str, Any]:
        """Валидирует и применяет allowlist-патч launcher config."""
        if not isinstance(patch, dict):
            raise ValueError("launcher должен быть объектом")
        allowed = {"autostart", "hotkey", "greeting_enabled"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"Недопустимые поля launcher: {', '.join(sorted(unknown))}")

        launcher = self._settings.launcher
        if "autostart" in patch:
            v = patch["autostart"]
            if not isinstance(v, bool):
                raise ValueError("autostart должен быть boolean")
            launcher.autostart = v
            self._apply_autostart(v)

        if "hotkey" in patch:
            v = patch["hotkey"]
            if not isinstance(v, str) or not v.strip():
                raise ValueError("hotkey должен быть непустой строкой")
            launcher.hotkey = v.strip()

        if "greeting_enabled" in patch:
            v = patch["greeting_enabled"]
            if not isinstance(v, bool):
                raise ValueError("greeting_enabled должен быть boolean")
            launcher.greeting_enabled = v

        self._settings.save_config()
        return self._launcher_payload()

    @staticmethod
    def _apply_autostart(enable: bool) -> None:
        """HKCU\\...\\Run — автозапуск Jarvis при логоне пользователя.

        Запись/удаление через winreg; на других ОС — no-op (безопасно).
        """
        import sys
        if sys.platform != "win32":
            return
        try:
            import winreg  # type: ignore[import-untyped]
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            name = "JARVIS"
            exe = sys.executable  # python.exe из текущего venv
            # Команда: python -m core.ws_server из backend_workdir
            cmd = f'"{exe}" -m core.ws_server'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as hk:
                if enable:
                    winreg.SetValueEx(hk, name, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(hk, name)
                    except FileNotFoundError:
                        pass
        except Exception as exc:  # noqa: BLE001
            log.warning("HKCU autostart %s не удался: %s",
                        "запись" if enable else "удаление", exc)

    @staticmethod
    def _validate_cloud_provider(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("provider должен быть строкой")
        provider = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider):
            raise ValueError("provider должен содержать латинские буквы, цифры, '_' или '-'")
        if provider == "local":
            raise ValueError("provider 'local' нельзя назначить облачному профилю")
        return provider

    @staticmethod
    def _validate_base_url(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("base_url должен быть строкой")
        url = value.strip().rstrip("/")
        if not url or len(url) > 2048:
            raise ValueError("base_url должен содержать от 1 до 2048 символов")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url должен быть корректным http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url не должен содержать учётные данные")
        from core.network_guard import assert_safe_url
        assert_safe_url(url)
        return url

    @staticmethod
    def _validate_model(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("model должен быть строкой")
        model = value.strip()
        if not model or len(model) > 256:
            raise ValueError("model должен содержать от 1 до 256 символов")
        return model

    def _update_cloud_settings(self, patch: Any) -> Dict[str, Any]:
        """Валидирует и атомарно применяет allowlist-патч cloud-профиля."""
        if not isinstance(patch, dict):
            raise ValueError("settings должен быть объектом")

        allowed = {"provider", "base_url", "model", "api_key", "clear_api_key"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"Недопустимые поля settings: {', '.join(sorted(unknown))}")

        provider = self._settings.tier_providers.get("analyst")
        if "provider" in patch:
            provider = self._validate_cloud_provider(patch["provider"])

        endpoint = self._settings.api_endpoints.get(provider) or ""
        if "base_url" in patch:
            endpoint = self._validate_base_url(patch["base_url"])

        model = self._settings.model_tiers.get("analyst") or ""
        if "model" in patch:
            model = self._validate_model(patch["model"])

        clear_key = patch.get("clear_api_key", False)
        if not isinstance(clear_key, bool):
            raise ValueError("clear_api_key должен быть boolean")
        key_patch = patch.get("api_key")
        if key_patch is not None and not isinstance(key_patch, str):
            raise ValueError("api_key должен быть строкой")
        if isinstance(key_patch, str) and len(key_patch) > 512:
            raise ValueError("api_key не должен превышать 512 символов")

        # Не делаем частичную запись: весь набор валидирован до мутации.
        self._settings.tier_providers.analyst = provider
        self._settings.api_endpoints.__setattr__(provider, endpoint)
        self._settings.model_tiers.analyst = model
        if clear_key:
            self._settings.api_keys.__setattr__(provider, "")
        elif isinstance(key_patch, str) and key_patch.strip():
            self._settings.api_keys.__setattr__(provider, key_patch.strip())
        # Пустая password-строка означает «ключ не менять».

        self._settings.save_config()
        # Новые ключ/base URL должны использоваться следующим запросом, а не
        # остаться в кэше RemoteAPIBackend.
        from core.llm.factory import clear_backend_cache

        clear_backend_cache()
        return self._cloud_settings_payload()

    # ------------------------------------------------------------------
    # S3: снимок экрана — решение сервера, а не флаг клиента
    # ------------------------------------------------------------------
    #: Одна формулировка отказа на все пути: по ней ориентируется UI.
    SCREEN_DENIED = "screen capture requires an explicit user authorization"

    def _authority_api(self, name: str):
        """Метод полномочий у Orchestrator или None — основа fail-closed."""
        candidate = getattr(self._orch, name, None)
        return candidate if callable(candidate) else None

    async def _capture_authorized(self) -> Dict[str, Any]:
        """Кадр снимка, если грант действителен; иначе PermissionError.

        Проверка и захват идут одним вызовом ``execute_authorized``, то есть
        под замком хранилища: между «разрешено» и снимком нельзя вклиниться
        отзывом гранта. ``to_thread`` — потому что и захват экрана, и замок
        блокирующие: на цикле событий они остановили бы весь мост.
        """
        execute = self._authority_api("execute_authorized")
        if execute is None:
            raise PermissionError(self.SCREEN_DENIED)

        def _capture() -> Any:
            from core.vision.screen import ScreenCapture

            return ScreenCapture().capture(permission=True)

        decision, result = await asyncio.to_thread(
            execute, screen_capture_request(), _capture,
        )
        if not decision.allowed or result is None:
            raise PermissionError(f"{self.SCREEN_DENIED}: {decision.reason}")
        return {
            "type": "screen_capture",
            "text": result.text,
            "active_window": result.active_window,
            "url": result.url,
            "grant_id": decision.grant_id,
        }

    async def _handle_screen_capture(self, ws: Any) -> None:
        """Есть грант — снимок; нет — запрос подтверждения у пользователя."""
        check = self._authority_api("check_authority")
        if check is None:
            # Ядро без хранилища полномочий не может ничего разрешить.
            await ws.send(json.dumps({"type": "error", "message": self.SCREEN_DENIED}))
            return
        try:
            decision = await asyncio.to_thread(check, screen_capture_request())
        except Exception as exc:
            log.warning("Проверка полномочий на снимок экрана не удалась: %s", exc)
            await ws.send(json.dumps({"type": "error", "message": self.SCREEN_DENIED}))
            return
        if decision.allowed:
            try:
                await ws.send(json.dumps(await self._capture_authorized()))
            except Exception as exc:
                await ws.send(json.dumps({"type": "error",
                                          "message": _safe_error_text(exc)}))
            return
        # Отказ — это не ошибка, а вопрос: UI показывает подтверждение с
        # выбором объёма, и только ответ пользователя создаёт грант.
        confirmation_id = f"screen-{uuid4().hex}"
        with self._lock:
            self._pending_screen[confirmation_id] = {"client": id(ws)}
        await ws.send(json.dumps({
            "type": "confirmation_required",
            "confirmation_id": confirmation_id,
            "prompt": "Разрешить снимок экрана для локального распознавания текста?",
            "tool": "screen_capture",
            "risk": {"level": "medium", "reason": decision.reason},
            "scopes": list(SCREEN_SCOPES),
        }))

    async def _confirm_screen_capture(self, ws: Any, confirmation_id: str,
                                     approve: bool, scope: Any) -> None:
        """Ответ пользователя на запрос снимка: грант выдаётся здесь и только здесь."""
        with self._lock:
            pending = self._pending_screen.pop(confirmation_id, None)
        if pending is None:  # pragma: no cover - защита от повторного confirm
            return
        if not approve:
            await ws.send(json.dumps({"type": "error", "message": self.SCREEN_DENIED}))
            return
        requested = str(scope or "once").strip().casefold()
        if requested not in SCREEN_SCOPES:
            # Неизвестный объём не трактуется «как разрешение поменьше»:
            # клиент прислал то, чего в протоколе нет.
            await ws.send(json.dumps({"type": "error",
                                      "message": f"unknown scope: {requested}"}))
            return
        issue = self._authority_api("issue_authority")
        revoke = self._authority_api("revoke_authority")
        if issue is None:
            await ws.send(json.dumps({"type": "error", "message": self.SCREEN_DENIED}))
            return
        try:
            grant = await asyncio.to_thread(
                lambda: issue(
                    screen_capture_proposal(requested),
                    user_instruction=(
                        "пользователь подтвердил снимок экрана "
                        f"(объём: {requested})"
                    ),
                    source_id=confirmation_id,
                    source_role="user",
                ),
            )
        except Exception as exc:
            log.warning("Грант на снимок экрана не выдан: %s", _safe_error_text(exc))
            await ws.send(json.dumps({"type": "error",
                                      "message": _safe_error_text(exc)}))
            return
        grant_id = getattr(grant, "grant_id", None)
        if requested == "session" and grant_id:
            with self._lock:
                self._session_screen_grants.setdefault(id(ws), set()).add(grant_id)
        try:
            await ws.send(json.dumps(await self._capture_authorized()))
        except Exception as exc:
            await ws.send(json.dumps({"type": "error",
                                      "message": _safe_error_text(exc)}))
        finally:
            if requested == "once" and grant_id and revoke is not None:
                # «Один раз» действительно один раз: TTL здесь страховка,
                # а отзыв сразу после снимка — само правило.
                try:
                    await asyncio.to_thread(revoke, grant_id, "scope=once spent")
                except Exception as exc:  # pragma: no cover - хранилище на диске
                    log.warning("Не отозван одноразовый грант на снимок: %s", exc)

    def _revoke_session_screen_grants(self, ws: Any) -> None:
        """Отключение клиента закрывает его гранты «на сессию»."""
        with self._lock:
            grant_ids = self._session_screen_grants.pop(id(ws), set())
            stale = [cid for cid, item in self._pending_screen.items()
                     if item.get("client") == id(ws)]
            for cid in stale:
                self._pending_screen.pop(cid, None)
        revoke = self._authority_api("revoke_authority")
        if revoke is None:
            return
        for grant_id in grant_ids:
            try:
                revoke(grant_id, "ws client disconnected")
            except Exception as exc:  # pragma: no cover - хранилище на диске
                log.warning("Не отозван сессионный грант на снимок: %s", exc)

    async def _on_message(self, ws, raw: str) -> None:
        if self._auth_token and ws not in self._authorized_clients:
            await ws.send(json.dumps({"type": "error", "message": "authentication required"}))
            return
        if not self._rate_allowed(ws):
            await ws.send(json.dumps({"type": "error", "message": "rate limit exceeded"}))
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"type": "error", "message": "bad json"}))
            return

        mtype = (msg.get("type") or "").lower()
        command_channel = "text"
        command_implicit_address = True
        if mtype == "voice_input":
            text = (msg.get("text") or "").strip()
            confidence = msg.get("confidence", 1.0)
            if isinstance(confidence, (int, float)) and confidence < 0.7:
                await ws.send(json.dumps({"type": "event", "event": {"type": "event:system", "payload": {"content": "Не расслышал, повтори."}, "timestamp": _now_ms()}}))
                return
            mtype = "command"
            msg["text"] = text
            command_channel = "voice"
            command_implicit_address = time.monotonic() <= self._voice_addressed_until
        elif mtype == "wake_word_detected":
            self._voice_addressed_until = time.monotonic() + 90.0
            self._emit({"type": "event", "event": {"type": "event:wake_word_detected", "payload": {}, "timestamp": _now_ms()}})
            return
        elif mtype == "voice_listen":
            if not getattr(getattr(self._settings, "stt", None), "enabled", False) and not getattr(getattr(self._settings, "voice", None), "stt_enabled", False):
                return
            loop = self._loop
            def _listen() -> None:
                try:
                    if self._stt_engine is None: return
                    text, confidence = self._stt_engine.transcribe_with_confidence_from_mic()
                    self._emit({"type": "voice_input", "text": text, "confidence": confidence})
                except Exception as exc:
                    self._emit({"type": "error", "message": _safe_error_text(exc)})
            if loop is not None: loop.call_soon_threadsafe(lambda: loop.run_in_executor(None, _listen))
            return
        if mtype == "settings:get":
            await ws.send(json.dumps({"type": "settings", "settings": self._cloud_settings_payload()}))
        elif mtype == "settings:update":
            try:
                settings = self._update_cloud_settings(msg.get("settings"))
            except Exception as exc:
                # Не логируем payload: в нём мог быть API-ключ.
                log.warning("Отклонено обновление cloud-настроек: %s", _safe_error_text(exc))
                await ws.send(json.dumps({"type": "error", "message": _safe_error_text(exc)}))
                return
            await ws.send(json.dumps({"type": "settings:saved", "ok": True, "settings": settings}))
        elif mtype == "command":
            text = (msg.get("text") or "").strip()
            if not text:
                return
            # Sprint 5 STEP 5.3 (barge-in): новый ввод прерывает текущую
            # речь Джарвиса — новая команда важнее договариваемой фразы.
            self._tts_interrupt()
            # Единый путь (P5 §5.7): тот же handle_input, что и в REPL.
            # Гоним через executor, чтобы не блокировать asyncio-цикл
            # во время модельного inference, и ловим возвращаемый state,
            # чтобы не потерять confirmation_id (P5 §5.6) при синхронном
            # fast-path (mission=None -> EVENT_CONFIRMATION_REQUIRED не
            # эмитится в TaskEvent).
            loop = self._loop

            def _dispatch(t: str, *, channel: str = "text",
                          implicit_address: bool = True) -> None:
                # Sprint 1 STEP 1/2: один correlation ID на весь логический
                # ответ. Sink принимает кумулятивный текст реального SSE-потока
                # модели и транслирует его в event:jarvis:token. Если бэкенд
                # не поддерживает streaming — sink ни разу не вызовется и
                # ответ пойдёт классическим start/end через _cb (fallback).
                tls = self._tls
                rid = uuid4().hex
                tls.cid = rid
                tls.started = False
                tls.ended = False
                tls.streamed = ""

                def _sink(visible: str) -> None:
                    tls.streamed = visible
                    if not tls.started:
                        tls.started = True
                        self._emit({
                            "type": "event",
                            "event": {
                                "type": "event:jarvis:start",
                                "payload": {"id": tls.cid, "kind": "jarvis", "content": ""},
                                "timestamp": _now_ms(),
                            },
                        })
                    self._emit({
                        "type": "event",
                        "event": {
                            "type": "event:jarvis:token",
                            "payload": {"id": tls.cid, "token": visible},
                            "timestamp": _now_ms(),
                        },
                    })

                install = getattr(self._orch, "install_stream_sink", None)
                try:
                    # The socket is deliberately available before the
                    # configured brain finishes its readiness probe. Hold the command in this
                    # executor worker until that one-time readiness event is
                    # complete; otherwise the first real request is reported
                    # as a false "not responding" result while llama-server
                    # is still loading the bundled GGUF.
                    wait_ready = getattr(self._orch, "wait_for_runtime_ready", None)
                    if callable(wait_ready):
                        self._emit({"type": "state", "state": "loading_model"})
                        ready_state = wait_ready()
                        if str(ready_state) != "ready":
                            diagnostics = {}
                            try:
                                diagnostics = dict(self._orch.runtime_diagnostics() or {})
                            except Exception:
                                diagnostics = {}
                            warmup = diagnostics.get("warmup") if isinstance(diagnostics, dict) else {}
                            detail = warmup.get("error") if isinstance(warmup, dict) else None
                            detail = detail or f"state={ready_state}"
                            raise RuntimeError(f"runtime не готов: {detail}")
                    if callable(install):
                        install(_sink)
                    if channel == "voice" and hasattr(self._orch, "cognitive"):
                        state = self._orch.handle_input(
                            t, channel="voice", implicit_address=implicit_address,
                        )
                    else:
                        state = self._orch.handle_input(t)
                    if isinstance(state, dict) and state.get("route"):
                        # Understanding Layer (Фаза 1): фронт узнаёт маршрут
                        # из бека, а не из собственного needsMission() —
                        # источник истины один. Шаг 7: рядом с маршрутом
                        # идёт полное решение semantic-роутера.
                        route_event: Dict[str, Any] = {
                            "type": "route",
                            "route": state["route"],
                        }
                        routing = state.get("routing_decision")
                        if isinstance(routing, dict):
                            route_event["decision"] = {
                                "tier": routing.get("tier"),
                                "kind": routing.get("kind"),
                                "tool": routing.get("tool"),
                                "confidence": routing.get("confidence"),
                                "margin": routing.get("margin"),
                                "top3": [
                                    name for name, _ in
                                    (routing.get("candidates") or [])[:3]
                                ],
                                "risk": routing.get("risk"),
                                "needs_confirmation": routing.get(
                                    "needs_confirmation"),
                                "latency_ms": routing.get("latency_ms"),
                                "llm_available": routing.get("llm_available"),
                            }
                        self._emit(route_event)
                    if isinstance(state, dict) and state.get("mission_id"):
                        # Фронт привязывает будущие события миссии (progress,
                        # steps, result) к запросу по mission_id. Раньше ACK
                        # уходил без него — привязка была невозможна (дыра D2).
                        self._emit({
                            "type": "mission:ack",
                            "mission_id": state["mission_id"],
                        })
                    if isinstance(state, dict) and state.get("tool"):
                        tool_name = str(state.get("tool") or "")
                        verified = bool(state.get("verified"))
                        output = state.get("assistant_output")
                        debug = output.get("debug", {}) if isinstance(output, dict) else {}
                        verification = debug.get("verification") if isinstance(debug, dict) else None
                        self._emit({
                            "type": "event",
                            "event": {
                                "type": "event:tool",
                                "payload": {
                                    "id": f"tool-{uuid4().hex}",
                                    "kind": "tool",
                                    "tool": tool_name,
                                    "content": tool_name,
                                    "status": "completed" if verified else "failed",
                                    "verified": verified,
                                    "verification": verification,
                                },
                                "timestamp": _now_ms(),
                            },
                        })
                        self._emit({
                            "type": "event",
                            "event": {
                                "type": "event:result",
                                "payload": {
                                    "id": f"result-{uuid4().hex}",
                                    "kind": "result",
                                    "content": str(state.get("response") or ""),
                                    "status": "verified" if verified else "failed",
                                    "verified": verified,
                                    "verification": verification,
                                },
                                "timestamp": _now_ms(),
                            },
                        })
                    if isinstance(state, dict) and state.get("assistant_output"):
                        # Typed diagnostics channel for Developer Mode. The
                        # frontend has no speech synthesis path for this event.
                        self._emit({
                            "type": "assistant_output",
                            "output": state["assistant_output"],
                        })
                    if isinstance(state, dict) and state.get("needs_confirmation"):
                        self._emit({
                            "type": "confirmation_required",
                            "confirmation_id": state.get("confirmation_id"),
                            "prompt": state.get("response", ""),
                            "tool": "",
                            "risk": {},
                        })
                        self._emit({"type": "state", "state": "idle"})
                except Exception as exc:
                    log.error("Ошибка обработки команды из WS: %s", _safe_error_text(exc), exc_info=True)
                    self._emit({"type": "error", "message": _safe_error_text(exc)})
                finally:
                    clear = getattr(self._orch, "clear_stream_sink", None)
                    if callable(clear):
                        try:
                            clear()
                        except Exception:  # pragma: no cover
                            pass
                    # Пузырь остался открытым (экзотический путь без _cb):
                    # закрываем честно накопленным текстом.
                    if getattr(tls, "started", False) and not getattr(tls, "ended", False):
                        tls.ended = True
                        self._emit({
                            "type": "event",
                            "event": {
                                "type": "event:jarvis:end",
                                "payload": {
                                    "id": tls.cid,
                                    "kind": "jarvis",
                                    "content": tls.streamed or (
                                        "Ошибка DeepInfra/DeepSeek runtime: поток завершился без текста."
                                        if bool(getattr(self._settings, "deepseek_brain_mode", False))
                                        else "Ошибка локального runtime: поток завершился без текста."
                                    ),
                                    "model": (
                                        "deepseek-ai/DeepSeek-V4-Flash-0731"
                                        if bool(getattr(self._settings, "deepseek_brain_mode", False))
                                        else "local"
                                    ),
                                },
                                "timestamp": _now_ms(),
                            },
                        })
                        self._emit({"type": "state", "state": "idle"})
                    tls.cid = None
                    tls.started = False
                    tls.ended = False
                    tls.streamed = ""

            if loop is not None:
                loop.call_soon_threadsafe(
                    lambda t=text, c=command_channel, a=command_implicit_address:
                    loop.run_in_executor(
                        None, lambda: _dispatch(t, channel=c, implicit_address=a)
                    )
                )
        elif mtype == "confirm":
            cid = msg.get("confirmation_id")
            approve = bool(msg.get("approve", False))
            if cid and cid in self._pending_screen:
                # S3: подтверждения снимка экрана живут в мосте, а не в
                # Orchestrator: их порождает протокол, а не план агента.
                await self._confirm_screen_capture(ws, str(cid), approve,
                                                   msg.get("scope"))
            elif cid:
                # Sprint 1 STEP 3: подтверждение запускает реальное выполнение
                # инструмента — тоже через executor, чтобы не блокировать цикл.
                loop = self._loop

                def _dispatch_confirm(cid_: str, approve_: bool) -> None:
                    try:
                        state = self._orch.answer_confirmation(cid_, approve_)
                        if not isinstance(state, dict):
                            return
                        tool_name = str(state.get("tool") or "")
                        verified = bool(state.get("verified"))
                        output = state.get("assistant_output")
                        debug = output.get("debug", {}) if isinstance(output, dict) else {}
                        verification = debug.get("verification") if isinstance(debug, dict) else None
                        if tool_name:
                            self._emit({
                                "type": "event",
                                "event": {
                                    "type": "event:tool",
                                    "payload": {
                                        "id": f"tool-{uuid4().hex}",
                                        "kind": "tool",
                                        "tool": tool_name,
                                        "content": tool_name,
                                        "status": "completed" if verified else "failed",
                                        "verified": verified,
                                        "verification": verification,
                                    },
                                    "timestamp": _now_ms(),
                                },
                            })
                            self._emit({
                                "type": "event",
                                "event": {
                                    "type": "event:result",
                                    "payload": {
                                        "id": f"result-{uuid4().hex}",
                                        "kind": "result",
                                        "content": str(state.get("response") or ""),
                                        "status": "verified" if verified else "failed",
                                        "verified": verified,
                                        "verification": verification,
                                    },
                                    "timestamp": _now_ms(),
                                },
                            })
                        if isinstance(output, dict):
                            self._emit({"type": "assistant_output", "output": output})
                        if state.get("needs_confirmation"):
                            self._emit({
                                "type": "confirmation_required",
                                "confirmation_id": state.get("confirmation_id"),
                                "prompt": state.get("response", ""),
                                "tool": tool_name,
                                "risk": {},
                            })
                        self._emit({"type": "state", "state": "idle"})
                    except Exception as exc:
                        log.error("Ошибка ответа на подтверждение из WS: %s", exc)

                if loop is not None:
                    # Handler already runs on the server loop: await the
                    # executor future so the confirmation is delivered before
                    # the next client message can race it.
                    await loop.run_in_executor(None, _dispatch_confirm, cid, approve)
                else:
                    _dispatch_confirm(cid, approve)
        elif mtype == "interrupt":
            # Отменяем все активные миссии (best-effort).
            try:
                for m in self._orch.list_missions(include_terminal=False):
                    self._orch.cancel_mission(m.task_id)
            except Exception as exc:  # pragma: no cover
                log.debug("interrupt error: %s", exc)
        elif mtype == "hotkey_pressed":
            # Sprint 5 STEP 4: overlay открыт глобальным hotkey — текущая
            # речь прерывается (пользователь перебивает, чтобы ввести новое).
            self._tts_interrupt()
        elif mtype == "ambient_initiated":
            # Sprint 6: system sources can ask the already-connected UI to
            # present a proactive phrase.  It is deliberately an event, not
            # a command: it never enters the action/conversation router.
            text = (msg.get("text") or "").strip()
            if text:
                self._emit({
                    "type": "event",
                    "event": {
                        "type": "event:system_initiated",
                        "payload": {"text": text},
                        "timestamp": _now_ms(),
                    },
                })
                # S5: _speak принимает только AssistantOutput — сырая строка
                # раньше отклонялась с TypeError и молча глоталась на debug.
                from core.voice.output import AssistantOutput
                self._speak(AssistantOutput.natural(text))
        elif mtype == "screen_capture":
            # S3: флага permission в протоколе больше нет — разрешение
            # спрашивается у хранилища полномочий, а не у клиента.
            await self._handle_screen_capture(ws)
        elif mtype == "first_launch":
            # The ritual owns exactly one durable datum: the name.  Save it
            # through the existing profile store instead of inventing a
            # second onboarding database or changing command routing.
            name = (msg.get("name") or "").strip()
            if name:
                try:
                    from core.memory.profile import update_profile
                    update_profile(self._settings, "name", name)
                except Exception as exc:  # pragma: no cover - disk edge
                    log.warning("Не удалось сохранить имя первого запуска: %s", exc)
        elif mtype == "launcher:get":
            await ws.send(json.dumps({"type": "launcher", "launcher": self._launcher_payload()}))
        elif mtype == "launcher:update":
            try:
                updated = self._update_launcher(msg.get("launcher"))
            except Exception as exc:
                log.warning("Отклонено обновление launcher: %s", _safe_error_text(exc))
                await ws.send(json.dumps({"type": "error", "message": _safe_error_text(exc)}))
                return
            await ws.send(json.dumps({"type": "launcher:saved", "ok": True,
                                      "launcher": updated}))
        elif mtype == "ping":
            await ws.send(json.dumps({"type": "pong"}))

    # ----------------------------------------------------------------- #
    #  Исходящие сообщения
    # ----------------------------------------------------------------- #
    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload)
        with self._lock:
            targets = list(self._clients)
        for ws in targets:
            try:
                # Sprint 1 STEP 3: один медленный/зависший клиент не должен
                # задерживать рассылку остальным (стриминг не должен
                # «замерзать» из-за backpressure одного соединения).
                await asyncio.wait_for(ws.send(data), timeout=5.0)
            except Exception:  # pragma: no cover - dropped/медленный клиент
                pass

    def _emit(self, payload: Dict[str, Any]) -> None:
        """Потокобезопасно шлёт сообщение из любого потока."""
        loop = self._loop
        if loop is None or not self._running:
            return
        try:
            loop.call_soon_threadsafe(
                lambda p=payload: asyncio.ensure_future(self._broadcast(p))
            )
        except Exception:  # pragma: no cover - loop закрыт
            # Sprint 1: потеря события больше не «тихая» — иначе UI слепнет
            # без следа в логах.
            log.warning("WS emit не удался (event loop закрыт?): type=%s",
                        payload.get("type"))

    def _on_task_event(self, event: TaskEvent) -> None:
        """Подписка на TaskRuntime -> трансляция в WS."""
        et = event.event_type
        payload = event.payload or {}

        if et in (EVENT_TASK_STARTED, EVENT_ACKNOWLEDGED):
            self._emit({"type": "state", "state": "thinking"})
        elif et == EVENT_TASK_COMPLETED:
            self._emit({"type": "state", "state": "idle"})
        elif et == EVENT_TASK_FAILED:
            self._emit({"type": "state", "state": "error"})
            if event.task_id in self._streaming_started:
                # Sprint 1 STEP 5: стримленный пузырь не должен висеть
                # открытым — закрываем понятным текстом, детали в лог.
                self._streaming_started.discard(event.task_id)
                self._emit({
                    "type": "event",
                    "event": {
                        "type": "event:jarvis:end",
                        "payload": {
                            "id": event.task_id,
                            "kind": "jarvis",
                            "content": f"Ошибка выполнения миссии: {payload.get('error') or 'точная причина не передана backend.'}",
                            "model": self._brain_model_label(),
                        },
                        "timestamp": _now_ms(),
                    },
                })

        if et == EVENT_STREAM_CHUNK:
            # Фронт создаёт пузырь ответа только после event:jarvis:start
            # (useBackendBridge). Шлём start ровно один раз — перед первым
            # токеном этого task_id.
            if event.task_id not in self._streaming_started:
                self._streaming_started.add(event.task_id)
                self._emit({
                    "type": "event",
                    "event": {
                        "type": "event:jarvis:start",
                        "payload": {
                            "id": event.task_id,
                            "kind": "jarvis",
                            "content": "",
                        },
                        "timestamp": _now_ms(),
                    },
                })
            self._emit({
                "type": "event",
                "event": {
                    "type": "event:jarvis:token",
                    "payload": {"id": event.task_id, "token": payload.get("text", "")},
                    "timestamp": _now_ms(),
                },
            })
        elif et == EVENT_STREAM_END:
            self._emit({
                "type": "event",
                "event": {
                    "type": "event:jarvis:end",
                    "payload": {
                        "id": event.task_id,
                        "content": payload.get("text", ""),
                        "model": payload.get("model", self._brain_model_label()),
                    },
                    "timestamp": _now_ms(),
                },
            })
        elif et == EVENT_CONFIRMATION_REQUIRED:
            self._emit({
                "type": "confirmation_required",
                "confirmation_id": payload.get("confirmation_id"),
                "prompt": payload.get("prompt", ""),
                "tool": payload.get("tool", ""),
                "risk": payload.get("risk", {}),
            })
            self._emit({"type": "state", "state": "idle"})
        elif et == EVENT_TASK_COMPLETED:
            result = payload.get("result", "")
            if event.task_id in self._streaming_started:
                # Sprint 1: миссия стримилась — пузырь с этим task_id уже
                # закрыт финальным output_callback (consume_streamed_mission).
                # Карточку-дубль не создаём.
                self._streaming_started.discard(event.task_id)
                if payload.get("status") == "cancelled":
                    self._emit({
                        "type": "event",
                        "event": {
                            "type": "event:jarvis:end",
                            "payload": {
                                "id": event.task_id,
                                "kind": "jarvis",
                                "content": "Задача отменена.",
                                "model": self._brain_model_label(),
                            },
                            "timestamp": _now_ms(),
                        },
                    })
                return
            # БАГ 2 FIX: _mission_runner уже вызвал _output_callback и
            # установил mission.metadata["_output_sent"] = True. Пузырь
            # уже создан через _cb — не создаём дублирующий.
            # Также проверяем _output_sent set (для стримленных миссий).
            if event.task_id in self._output_sent:
                self._output_sent.discard(event.task_id)
                return
            # Проверяем флаг в metadata миссии (для нестримленных миссий).
            try:
                mission_obj = self._orch.get_mission(event.task_id)
                if mission_obj is not None and mission_obj.metadata.get("_output_sent"):
                    return
            except Exception as exc:
                log.debug("get_mission для _output_sent check: %s", exc)
            # Для миссий без _output_callback (например, отменённых до запуска)
            # создаём пузырь из EVENT_TASK_COMPLETED только если есть result.
            if result:
                self._emit({
                    "type": "event",
                    "event": {
                        "type": "event:jarvis",
                        "payload": {
                            "id": event.task_id,
                            "kind": "result",
                            "content": result if isinstance(result, str) else "",
                        },
                        "timestamp": _now_ms(),
                    },
                })

    # ----------------------------------------------------------------- #
    #  Жизненный цикл
    # ----------------------------------------------------------------- #
    def start(self) -> None:
        if not _HAS_WS:
            log.error("websockets не установлен — WS-сервер недоступен")
            return
        if self._running:
            return
        # Запускаем оркестратор (он поднимет фоновые сервисы)
        self._orch.start()
        if self._system_monitor is not None:
            self._system_monitor.start()
        self._unsub = self._orch.subscribe_events(self._on_task_event)
        self._running = True
        self._stop_event.clear()

        def _run_loop() -> None:
            asyncio.run(self._serve_forever())

        self._thread = threading.Thread(target=_run_loop, name="jarvis-ws", daemon=False)
        self._thread.start()
        log.info("WS-сервер стартует на ws://%s:%s", self._host, self._port)

    async def _serve_forever(self) -> None:
        # ВАЖНО (P5 §5.2): этот метод ВСЕГДА вызывается из asyncio.run(),
        # который уже создал и запустил свой event-loop. Брать
        # asyncio.new_event_loop() здесь — ошибка: handler крутится на loop'е
        # asyncio.run, а self._loop указывал бы на другой, никогда не
        # запущенный loop, и все call_soon_threadsafe/_emit терялись бы.
        self._loop = asyncio.get_running_loop()
        self._ws_server = await serve(self._handler, self._host, self._port)  # type: ignore
        async with self._ws_server:
            log.info("WS-сервер слушает ws://%s:%s", self._host, self._port)
            await self._stop_event_async()

    async def _stop_event_async(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(0.05)

    def shutdown(self) -> None:
        self._running = False
        self._stop_event.set()
        loop = self._loop
        if loop is not None and self._ws_server is not None:
            try:
                loop.call_soon_threadsafe(self._ws_server.close)
            except Exception:
                pass
        if self._system_monitor is not None:
            self._system_monitor.stop()
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # pragma: no cover
                pass
        self._orch.shutdown()
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def join(self, timeout: float | None = None) -> bool:
        thread = getattr(self, "_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        return bool(thread is None or not thread.is_alive())


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Точка входа для автономного WS-сервера (без REPL).

    S2: аутентификация обязательна. Токен берётся из ``JARVIS_WS_TOKEN``, иначе
    из зашифрованного хранилища ``data/security/ws-token.dpapi`` (создаётся при
    первом запуске). Без токена сервер стартует ТОЛЬКО по явному
    ``JARVIS_WS_DEV_NOAUTH=1`` и печатает громкое предупреждение; если токен
    недоступен и флага нет — процесс не стартует.
    """
    if not _HAS_WS:
        raise RuntimeError("websockets не установлен")
    settings: Settings = load_config()
    try:
        from core.llm.hardware_profile import apply_profile
        apply_profile(settings, logger=log)
    except Exception as exc:
        log.warning("Автопрофиль локальной модели пропущен: %s", exc)
    settings.ensure_directories()
    from core.security.ws_token import ENV_DEV_NOAUTH, resolve_server_token

    try:
        auth_token, token_mode = resolve_server_token(settings)
    except RuntimeError as exc:
        raise RuntimeError(
            f"WS-сервер не запущен: нет токена аутентификации ({exc}). "
            f"Задайте JARVIS_WS_TOKEN или (только для разработки) {ENV_DEV_NOAUTH}=1."
        ) from exc
    orch = Orchestrator(settings)
    configured_origins = {
        item.strip().rstrip("/")
        for item in os.environ.get("JARVIS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    log.info("WS-сервер: аутентификация=%s, origins=%d",
             token_mode, len(configured_origins or _DEFAULT_ALLOWED_ORIGINS))
    server = JarvisWSServer(orch, host=host, port=port,
                            auth_token=auth_token,
                            allowed_origins=configured_origins or set(_DEFAULT_ALLOWED_ORIGINS))
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


def _print_ws_token() -> int:
    """``--print-ws-token``: выдать токен лаунчеру (одна строка в stdout).

    Используется Tauri-лаунчером один раз при старте: он читает stdout и
    отдаёт значение webview по ``invoke("ws_auth_token")``. Так токен не
    попадает ни в URL, ни в аргументы командной строки backend-процесса.
    """
    from core.security.ws_token import load_or_create_token

    try:
        print(load_or_create_token(load_config()), flush=True)
    except RuntimeError as exc:
        print(f"ws-token error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    if "--print-ws-token" in sys.argv[1:]:
        raise SystemExit(_print_ws_token())
    run_server()
