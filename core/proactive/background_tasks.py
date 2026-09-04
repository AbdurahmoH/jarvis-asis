"""Фоновые задачи (BackgroundScheduler).

Обёртка над TaskManager (напоминания) + периодическая задача
«ночная консолидация» (раз в сутки — заглушка, логирует,
что можно расширить позже анализом дня).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dt_time
from typing import Callable, Optional

from config.settings import Settings
from core.actions.reminders import TaskManager
from core.utils.logger import get_logger

__all__ = ["BackgroundScheduler"]

log = get_logger(__name__)


class BackgroundScheduler:
    """Планировщик фоновых задач."""

    def __init__(
        self,
        settings: Settings,
        task_manager: TaskManager,
        nightly_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            settings: конфигурация.
            task_manager: менеджер напоминаний (для проверки срабатываний).
            nightly_callback: функция для ночной консолидации (вызывается раз в сутки).
        """
        self._settings = settings
        self._task_manager = task_manager
        self._nightly_callback = nightly_callback

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

        # Время ночной консолидации (по умолчанию 03:00)
        nightly_str = getattr(getattr(settings, "proactive", None), "nightly_time", "03:00")
        try:
            h, m = map(int, nightly_str.split(":"))
            self._nightly_time = dt_time(h, m)
        except Exception:
            self._nightly_time = dt_time(3, 0)

    def start(self) -> None:
        """Запускает планировщик."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="BackgroundScheduler")
            self._thread.start()
            log.info("BackgroundScheduler запущен (nightly=%s)", self._nightly_time.strftime("%H:%M"))

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """Останавливает планировщик."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop.set()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        log.info("BackgroundScheduler остановлен")

    def join(self, timeout: float | None = None) -> bool:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return bool(self._thread is None or not self._thread.is_alive())

    # --------------------------------------------------------------------- #
    #  Цикл
    # --------------------------------------------------------------------- #

    def _loop(self) -> None:
        last_nightly_date = None

        while self._running and not self._stop.is_set():
            try:
                now = datetime.now()

                # 1. Проверка напоминаний (TaskManager использует свои таймеры,
                #    но можем дополнительно проверять здесь для надежности)
                self._check_reminders()

                # 2. Ночная консолидация
                today = now.date()
                if last_nightly_date != today and now.time() >= self._nightly_time:
                    self._run_nightly()
                    last_nightly_date = today

            except Exception as exc:
                log.error("BackgroundScheduler loop ошибка: %s", exc)

            self._stop.wait(30)  # проверка каждые 30 сек

    def _check_reminders(self) -> None:
        """Проверяет и обрабатывает сработавшие напоминания.

        TaskManager использует threading.Timer, поэтому.callback
        сработает автоматически. Этот метод — для дополнительной страховки
        и логирования.
        """
        reminders = self._task_manager.list_reminders()
        now = time.time()
        for r in reminders:
            if r["remaining_sec"] <= 0:
                log.debug("Напоминание #%s должно сработать (remaining=%ds)",
                          r["id"], r["remaining_sec"])

    def _run_nightly(self) -> None:
        """Запускает ночную консолидацию."""
        log.info("Запуск ночной консолидации...")
        if self._nightly_callback:
            try:
                self._nightly_callback()
            except Exception as exc:
                log.error("Nightly callback ошибка: %s", exc)
        else:
            # Заглушка: просто логируем
            log.info("Ночная консолидация: заглушка. Здесь можно добавить:")
            log.info("  - Анализ дня (статистика, паттерны)")
            log.info("  - Консолидация памяти (дедуп, суммаризация)")
            log.info("  - Подготовка утреннего брифинга")
            log.info("  - Очистка старых временных файлов")
