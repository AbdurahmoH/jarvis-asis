"""Проактивный движок (Proactor).

Фоновый цикл в отдельном потоке, который периодически решает,
стоит ли инициировать сообщение пользователю.

Триггеры:
1. Сработавшее напоминание (callback от TaskManager).
2. «Скучающий» таймер — если пользователь неактивен N минут
   (лимит из ``settings.limits.proactive_cooldown_min``).

При срабатывании генерирует фразу через CouncilRouter/LocalFace
и кладёт в очередь вывода (callback, который подключит main.py).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from config.settings import Settings
from core.router import CouncilRouter
from core.state import new_state
from core.utils.logger import get_logger

__all__ = ["Proactor"]

log = get_logger(__name__)


class Proactor:
    """Фоновый проактивный цикл."""

    def __init__(
        self,
        settings: Settings,
        council: CouncilRouter,
        output_callback: Callable[[str], None],
        reminder_check_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        Args:
            settings: конфигурация.
            council: CouncilRouter для генерации фраз.
            output_callback: функция вывода сообщения пользователю (текст -> None).
                Например, отправка в TTSQueue или печать в консоль.
            reminder_check_callback: опциональная функция, возвращающая True,
                если есть сработавшие напоминания (оркестратор прокидывает сюда
                TaskManager.check_due).
        """
        self._settings = settings
        self._council = council
        self._output = output_callback
        self._reminder_check = reminder_check_callback

        self._cooldown_min = getattr(getattr(settings, "limits", None), "proactive_cooldown_min", 30)
        self._interval_sec = 60  # проверка раз в минуту

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._last_user_activity = time.time()
        self._last_proactive_time = 0.0

    def start(self) -> None:
        """Запускает фоновый поток."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="Proactor")
            self._thread.start()
            log.info("Proactor запущен (cooldown=%d мин, interval=%d сек)", self._cooldown_min, self._interval_sec)

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """Останавливает фоновый поток."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop.set()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        log.info("Proactor остановлен")

    def join(self, timeout: float | None = None) -> bool:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return bool(self._thread is None or not self._thread.is_alive())

    @property
    def stopped(self) -> bool:
        return not bool(self._thread and self._thread.is_alive())

    def mark_user_activity(self) -> None:
        """Вызывается при активности пользователя (ввод текста, голос и т.д.)."""
        self._last_user_activity = time.time()

    def trigger_reminder(self, text: str) -> None:
        """Принудительно триггерит проактивное сообщение (например, от напоминания)."""
        self._speak_proactive(text, source="reminder")

    # --------------------------------------------------------------------- #
    #  Основной цикл
    # --------------------------------------------------------------------- #

    def _loop(self) -> None:
        while self._running and not self._stop.is_set():
            try:
                self._check_triggers()
            except Exception as exc:
                log.error("Proactor loop ошибка: %s", exc)
            self._stop.wait(self._interval_sec)

    def _check_triggers(self) -> None:
        now = time.time()

        # 1. Проверка напоминаний (есть callback)
        if self._reminder_check:
            try:
                if self._reminder_check():
                    # Напоминание сработало — callback уже должен был вывести текст
                    # Но можем добавить свой контекст
                    pass
            except Exception as exc:
                log.warning("Proactor reminder_check ошибка: %s", exc)

        # 2. Скучающий таймер (пользователь неактивен давно)
        idle_sec = now - self._last_user_activity
        cooldown_sec = self._cooldown_min * 60
        since_last_proactive = now - self._last_proactive_time

        if idle_sec >= cooldown_sec and since_last_proactive >= cooldown_sec:
            # Генерация проактивной фразы
            idle_min = int(idle_sec // 60)
            prompt = (
                f"Пользователь неактивен {idle_min} минут. "
                "Скажи что-то краткое, полезное или забавное, чтобы вернуть внимание. "
                "Не спамь, не извиняйся. Обращение: сёр."
            )
            self._speak_proactive(prompt, source="idle")
            self._last_proactive_time = now

    def _speak_proactive(self, prompt: str, source: str) -> None:
        """Генерирует и выводит проактивное сообщение."""
        try:
            state = new_state(prompt)
            state = self._council.route(state)
            response = state.get("response", "").strip()
            if response:
                log.info("Proactor (%s): %s", source, response[:80])
                self._output(response)
            else:
                log.debug("Proactor (%s): пустой ответ", source)
        except Exception as exc:
            log.error("Proactor генерация ошибка: %s", exc)
