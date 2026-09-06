"""Fast provider health accounting and bounded circuit breaking."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import HealthSnapshot, HealthStatus


@dataclass
class _State:
    status: HealthStatus = HealthStatus.AVAILABLE
    latency_ms: float | None = None
    failures: int = 0
    consecutive_failures: int = 0
    timeouts: int = 0
    recent_success: bool = False
    last_error: str = ""
    opened_at: float = 0.0
    #: C3: временные метки отказов в скользящем окне для оконного гейта.
    failure_times: deque = field(default_factory=lambda: deque(maxlen=64))
    #: C3: half-open уже разрешил пробу, но результат ещё не зафиксирован.
    half_open: bool = False


class BrainHealthManager:
    """Учёт здоровья провайдеров и ограниченный circuit breaker.

    C3: breaker оконный — ``failure_threshold`` отказов/таймаутов за
    ``failure_window_seconds`` открывают цепь на ``cooldown_seconds``, затем
    разрешается одна half-open проба; успех закрывает цепь.
    """

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 90.0,
                 failure_window_seconds: float = 60.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.failure_window_seconds = max(0.0, float(failure_window_seconds))
        self._states: dict[str, _State] = {}
        self._lock = threading.RLock()

    def _state(self, key: str) -> _State:
        return self._states.setdefault(key, _State())

    def record_success(self, key: str, *, latency_ms: float) -> None:
        with self._lock:
            state = self._state(key)
            state.status = HealthStatus.AVAILABLE
            state.latency_ms = float(latency_ms)
            state.consecutive_failures = 0
            state.recent_success = True
            state.last_error = ""
            state.opened_at = 0.0
            state.failure_times.clear()
            state.half_open = False

    def record_failure(self, key: str, *, latency_ms: float | None = None,
                       timeout: bool = False, error: str = "") -> None:
        with self._lock:
            state = self._state(key)
            state.failures += 1
            state.consecutive_failures += 1
            state.timeouts += int(timeout)
            state.recent_success = False
            state.latency_ms = latency_ms
            state.last_error = str(error)[:240]
            now = time.monotonic()
            state.failure_times.append(now)
            window = self.failure_window_seconds
            while state.failure_times and now - state.failure_times[0] > window:
                state.failure_times.popleft()
            if (state.consecutive_failures >= self.failure_threshold
                    or len(state.failure_times) >= self.failure_threshold):
                state.status = HealthStatus.UNHEALTHY
                state.opened_at = now
                state.half_open = False
            else:
                state.status = HealthStatus.DEGRADED

    def allow(self, key: str) -> bool:
        """Разрешить ли запрос: closed/half-open — да, open — нет.

        После истечения cooldown состояние становится half-open и одна проба
        проходит (это и есть «half-open probe» наряда).
        """
        with self._lock:
            state = self._state(key)
            if state.status is not HealthStatus.UNHEALTHY:
                return state.status is not HealthStatus.OFFLINE
            if time.monotonic() - state.opened_at >= self.cooldown_seconds:
                if not state.half_open:
                    state.half_open = True
                    return True
                return False
            return False

    def circuit_state(self, key: str) -> str:
        """C3: "closed" | "open" | "half_open" — для provider_effective."""
        with self._lock:
            state = self._state(key)
            if state.status is not HealthStatus.UNHEALTHY:
                return "closed"
            if state.half_open and time.monotonic() - state.opened_at >= self.cooldown_seconds:
                return "half_open"
            return "open"

    def force_retry(self, key: str) -> None:
        with self._lock:
            state = self._state(key)
            state.status = HealthStatus.DEGRADED
            state.opened_at = 0.0

    def set_status(self, key: str, status: HealthStatus) -> None:
        with self._lock:
            self._state(key).status = status

    def snapshot(self, key: str) -> HealthSnapshot:
        with self._lock:
            state = self._state(key)
            return HealthSnapshot(
                status=state.status, latency_ms=state.latency_ms,
                failures=state.failures, timeouts=state.timeouts,
                recent_success=state.recent_success, last_error=state.last_error,
            )


class ProviderProbe:
    """C3: фоновый health-probe провайдеров мозга.

    Каждые ``interval_sec`` дергает ``refresh_health`` хранилища снапшотов;
    дёшево (HEAD-подобный /models c таймаутом внутри provider.health()),
    живёт в daemon-потоке и останавливается без join-зависаний.
    """

    def __init__(self, refresh, *, interval_sec: float = 60.0) -> None:
        self._refresh = refresh
        self._interval = max(5.0, float(interval_sec))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            # Первая проба — сразу при старте, дальше раз в interval.
            while not self._stop.is_set():
                try:
                    self._refresh()
                except Exception:
                    pass
                if self._stop.wait(self._interval):
                    break

        self._thread = threading.Thread(target=_loop, name="brain-provider-probe",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = ["BrainHealthManager", "ProviderProbe"]

