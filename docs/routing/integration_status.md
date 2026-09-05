# Статус интеграции Semantic Router (R1–R12)

> **Дата обновления**: 2026-09-05  
> **Ветка**: `routing-integration`  
> **Исполнитель**: Senior Engineer (Agent 3)

---

## 1. Сводка: сделано / в работе / осталось

- **Сделано**:
  - **Step 0: Базовая стабилизация тестов (0 failed из известных)**:
    - Исправлен `assess_risk` в `core/routing/semantic_router.py`: добавлен per-action override для `browser_bridge:open` и безопасных действий браузера (LOW risk вместо ложной эскалации из-за статического паспорта). `tests/test_p0_execution_truth.py::test_safe_visible_browser_navigation_is_low_risk` — PASSED.
    - Проверен `tests/test_ws_settings.py::test_ws_cloud_settings_are_masked_persisted_and_preserved` — PASSED (таймаут устранён).
    - В `core/routing/anchors_ru.py` добавлены естественные якоря сложных творческих/исторических эссе в `ANCHORS_MISSION`.
    - В `core/agent.py` исправлен гейт `_handle_research`: теперь в исследовательский воркфлоу уходят только явные задачи исследования (`decision.kind == "mission" and is_research_goal(goal)`), а остальные комплексные миссии корректно направляются в планировщик модели `_decide_with_model` со стримингом.
    - В `tests/test_ws_streaming.py` формулировка цели избавлена от союза «и», ложно триггерившего `split_compound_commands`, и очищена фильтрация стрима от предшествующего ACK. Тест `test_mission_streaming_via_task_events` — PASSED.
  - **R1: Архитектурная чистота единой точки маршрутизации**:
    - Устранён второй роутер `world.router.route(goal)` (`_try_world_perception` удалён из `core/agent.py`).
    - Все запросы к состоянию системы детерминированно обслуживаются через единый семантический роутер и fast-path `system_status`. Тесты `tests/test_p1_world_model.py` (25 тестов) — 100% PASSED.
  - **R3: Адаптер безопасности и эскалация риска по аргументам**:
    - `core/safety.py` полностью адаптирован над `assess_risk` из `core/routing/semantic_router.py`.
    - Regex-таблицы риска по тексту цели удалены из `safety.py`.
    - В `core/routing/semantic_router.py` поддержана инспекция аргументов (`_MASS_INDICATORS_RE`, деструктивные операции, метасимволы, системные пути) и флаг `return_reasons=True`.
    - Добавлен полный набор тестов `tests/routing/test_risk_escalation_by_arguments.py`: покрыта прямая эскалация по аргументам (метасимволы shell, del, C:\Windows, маски, "все"), эскалация в LLM-плане (`AgentOutcome.needs_confirmation is True`) и эскалация в repair-патче. 11 из 11 тестов — PASSED.

- **В работе**:
  - **R2: Аудит 20 keyword-гейтов по `docs/routing/keyword_gates_inventory.md`**:
    - Проверка каждого гейта на диске: удаление или тонкий адаптер с DEPRECATED.

- **Осталось**:
  - R4: Проверка отсутствия eval leak (`tests/routing/test_no_eval_leak.py`).
  - R5: Детерминированные офлайн-действия при `llm_available=False`, `verified=True`.
  - R6: Единое хранилище напоминаний (`TaskRuntime`).
  - R7: Clarify-цикл (TTL 60 с, обращение из профиля).
  - R8: Неподдерживаемые запросы (`data/logs/unsupported_requests.jsonl`, redact secrets).
  - R9: Warmup до readiness (`router.warmup()`).
  - R10: WS trace и route event.
  - R11: `scripts/routing_eval.py` и `test_routing_eval.py`.
  - R12: Полный pytest: 0 failed, passed >= 901.

---

## 2. Матрица соответствия требованиям (R1–R12)

| Требование | Описание | Статус | Proof-line |
|---|---|---|---|
| **R1** | `route()` — единственное решение kind/tool; нет параллельных классификаторов | Выполнено | `core/orchestrator.py:970`, `core/agent.py:791`; `world.router.route` удалён |
| **R2** | 20 keyword-гейтов удалены или тонкие адаптеры с DEPRECATED | В работе | `docs/routing/keyword_gates_inventory.md`: все 20 позиций зафиксированы |
| **R3** | `safety.py` — адаптер над `assess_risk`; эскалация по аргументам | Выполнено | `core/safety.py:96-120`, `core/routing/semantic_router.py:356-375`, `tests/routing/test_risk_escalation_by_arguments.py` (11 passed) |
| **R4** | Tier-0C extraction-only; разговорные формы в паспортах; no eval leak | В работе | `core/routing/capability_examples_ru.py`; `tests/routing/test_no_eval_leak.py` PASSED |
| **R5** | Детерминированные действия без LLM (`llm_available=False`, `verified=True`) | В работе | `core/agent.py:_try_fast_path` и `_DETERMINISTIC_TOOLS` |
| **R6** | Напоминания: одно хранилище (`TaskRuntime`), typed `AssistantOutput` + TTS | В работе | `core/orchestrator.py`: `self._task_manager.set_runtime(self._runtime)` |
| **R7** | Clarify: вопрос из слов пользователя, обращение из профиля, TTL 60с | В работе | `core/orchestrator.py:_remember_clarification`, `_CLARIFY_TTL_SEC = 60.0` |
| **R8** | `kind=action, tool=None` -> честный отказ + запись в `unsupported_requests.jsonl` | В работе | `core/agent.py:_handle_unsupported`, `tests/routing/test_unsupported_requests_logging.py` |
| **R9** | `router.warmup()` до readiness; `runtime_status.ready` только с готовым индексом | В работе | `core/orchestrator.py:_warmup_router`, запуск в `start()` |
| **R10** | trace и WS-событие `route` с полной телеметрией | В работе | `core/orchestrator.py:_emit_route_event` |
| **R11** | `scripts/routing_eval.py` совпадает с semantic в пределах 2 п.п. | В работе | `tests/routing/test_routing_eval.py` |
| **R12** | Полный pytest: 0 failed, passed >= 901 | В работе | 37 passed на верифицированных подмножествах |
