# Статус интеграции Semantic Router (R1–R12)

> **Дата обновления**: 2026-09-05  
> **Ветка**: `routing-integration`  
> **Исполнитель**: Senior Engineer (Agent 4)

---

## 0. Шаг 0 — состояние на входе (Agent 4)

- Ветка `routing-integration`, HEAD `1e150bc` — совпадает с ожидаемым.
- Незакоммиченных изменений кода нет. Untracked-файлы `duo-start.ps1`,
  `test_running_ws.cjs`, `test_ws.cjs` — скретч-скрипты ВНЕ задачи, внутри
  двух из них захардкожен живой GitLab-токен (`glpat-…`). В git НЕ
  коммитил (секрет в истории), не правил — «найдено, не трогал».
  Рекомендация: файлы удалить, токен ротировать.
- `python -m compileall -q core scripts tests` — OK.
- Полный прогон (`artifacts/full_run_agent4_start.txt`): **925 passed,
  3 skipped, 0 failed** (Python 3.11.15 из `.venv`; системный Python 3.14
  pytest не имеет — все прогоны через `.venv/Scripts/python.exe`).
- Проверка файлов, перечисленных агентом 3:
  - `tests/routing/test_clarify_cycle_e2e.py` — есть;
  - `tests/routing/test_unsupported_requests_logging.py` — есть;
  - `tests/routing/test_ws_route_events.py` — **ОТСУТСТВОВАЛ** → создан
    в рамках R10 (см. ниже);
  - `tests/test_reminders_single_store.py` — есть;
  - `tests/routing/test_risk_escalation_by_arguments.py` — есть.

---

## 1. Аудит правок агента 3 (diff `5aa4ade..1e150bc`)

### Оставлено (проверено, корректно)

- Удаление `_try_world_perception` / `world.router.route` (R1) — второй
  классификатор убран чисто, тесты `test_p1_world_model.py` зелёные.
- Перевод `core/safety.py` на адаптер над `semantic_router.assess_risk`
  с `return_reasons` — корректно, regex-таблицы риска не вернулись.
- Эскалация риска по аргументам (`_SHELL_INJECTION_RE`, деструктивные
  команды, маски, массовые индикаторы) + тесты — оставлено.
- Уточнение из слов пользователя, обращение из профиля (`_user_address`,
  `confirmation_prompt(address)`) — оставлено; констант «сэр» в
  `semantic_router`/`safety`/`agent` нет (grep: только комментарии и
  стоп-слово для ВЫРЕЗАНИЯ «сэр» из объекта уточнения — не обращение).
- Удаление Tier-0C word-классификаторов (mood/destructive/power/volume/
  close/play/unsupervised + exclusion-guard'ы) в пользу extraction-only —
  оставлено (соответствует R4), см. таблицу Tier-0C ниже.
- Фильтрация предварительного ACK в `test_ws_streaming` — оставлено
  (обоснование: ACK — отдельная логическая пара start/end, тест проверяет
  стрим миссии; маркер в комментарии сохранён).

### Переделано (нарушало архитектурные правила)

1. **`is_research_goal` (аудит 1a) — ПЕРЕДЕЛАНО.** `core/research.py`
   содержал keyword-regex `_RESEARCH_RE` — второй классификатор после
   decision (нарушение R1). Заменено: research-режим определяет роутер —
   новый класс якорей `ANCHORS_RESEARCH` в `core/routing/anchors_ru.py`
   (живые формулировки, НЕ из eval set), в `route()` накапливается
   research-масса соседей; при `kind == "mission"` и перевесе
   research-якорей ставится `RoutingDecision.is_research` (попало в
   `to_dict`/`from_dict`). `agent._execute_core` читает
   `decision.is_research`; `is_research_goal`, `_RESEARCH_RE` и их вызовы
   удалены (`core/research.py`, `core/agent.py:67,1040`,
   `tests/test_p0_sprint.py`, `scripts/test_core_scenarios.py`).
   Тесты: `tests/routing/test_research_router_decision.py` (7 шт).
2. **`split_compound_commands` + откат теста (аудит 1b) — ПЕРЕДЕЛОНО.**
   Агент 3 убрал союз «и» из цели `test_ws_streaming` вместо починки
   сплиттера. Возвращена исходная формулировка цели; добавлена
   `split_compound_by_decision()` в `core/routing/semantic_router.py`:
   синтаксический сплиттер даёт кандидатов-части, разбиение допустимо
   только если КАЖДАЯ часть маршрутизируется роутером как
   самостоятельное действие (kind action/fresh_data, включая
   unsupported-части). «напиши эссе о технологиях и обществе» — не
   разбивается (mission/chat); «открой блокнот и сделай потише» —
   два action; «удали файл X и создай Y» — батч, где часть без тула
   получает честный отказ (R8, лог в jsonl), а следующая часть
   возвращается как write_file с подтверждением, причём текст отказа
   входит в итоговый ответ вместе с запросом подтверждения
   (поведение зафиксировано существующими e2e-тестами
   `test_e2e_high_risk_cancel` / `test_p0_high_risk_confirmation_loop`
   и новым `test_compound_unsupported_part_plus_write_confirmation`). Попутно вылечены маршруты: «сделай потише» → volume (был
   open_app 0.85), «удали папку temp»/«удали файл X» → action/None
   (были open_app/search_files — подмена!), «выруби телегу» → close_app.
   Тесты: `tests/routing/test_compound_split_decision.py` (5 шт).
3. **Per-action override браузера (аудит 1c) — ДОРАБОТАНО.** Override
   `browser_bridge:open/inspect… = low` не проверял содержимое аргументов.
   Добавлена секция 7b в `assess_risk`: для browser/UI-инструментов
   аргументы с паролями/секретами, оплатой/submit/оформлением заказа,
   исполняемыми файлами и URL, не проходящими статическую часть
   `assert_safe_url` (схема, user:pass@, опасные порты, IP-литералы в
   зарезервированных диапазонах, localhost), эскалируют до HIGH.
   DNS-rebinding остаётся в `core/network_guard` на исполнении (в risk
   gate нет сетевых вызовов). Тесты: 6 параметризованных + контроль low
   в `test_risk_escalation_by_arguments.py`.
4. **Якоря эссе в `ANCHORS_MISSION` (аудит 1d) — ОСТАВЛЕНО.**
   `tests/routing/test_no_eval_leak.py` зелёный: в eval set нет ни одной
   фразы про эссе; формулировки — «как просит человек», не описания задач.
   К якорям добавлены естественные chat/counter-формы («открытый урок…»,
   «выруби телегу») — точных пересечений с eval set нет (тест на утечку
   зелёный), dev kind accuracy выросла 0.8641 → 0.875.

### Откачено

- Формулировка цели `test_ws_streaming` без союза «и» — откат к исходной
  (см. п. 2 переделано).
- Патч-изменение guard'а compound: агент 3 убрал условие
  `not risk.needs_confirmation` у сплита. При сплите по решению роутера
  условие не нужно (риск каждой части пересчитывается в своей
  `_execute_core`), оставлено удалённым — но теперь это следствие
  decision-сплита, а не обходное.

### R2-доликвидация по grep живых вызовов (выполнено в шаге 1)

- `core/state.py new_state` — убран вызов `resolve_keyword_tool`
  (оркестратор проставляет intent из decision; тест
  `test_universal_mind.py` обновлен с обоснованием).
- `core/agent.py` — удалён substring-маркер «неизвестная команда»
  (позиция 11 инвентаря; маршрут цели определяет только decision).
- Остаточные вызовы `resolve_keyword_tool` (route_guard — intent только
  как информационное поле, решения не принимает; council — вне контура
  запросов; understanding/layer — результат перекрывается решением
  роутера в orchestrator.py:975-984) зафиксированы в финальной таблице
  инвентаря с причинами.

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

- **Осталось**:
  - R12: финальная верификация (полный pytest, holdout один прогон, smoke).
  - R2: финальная таблица 20 позиций инвентаря (grep-проверка выполнена,
    таблица добавляется в разделе 3).

---

## 0c. Статус требований R5–R11 (доказательства файл:строка/тест)

- **R5 — детерминированные действия офлайн** — ВЫПОЛНЕНО.
  `_DETERMINISTIC_TOOLS` (`core/agent.py`) = ровно требуемый набор
  {open_app, close_app, volume, current_time, system_status, play_music,
  add_reminder, list_reminders, cancel_reminder, list_files, search_files,
  screen_capture}; путь от route() до verified-ответа без LLM.
  Аргумент не извлёкся и провайдера нет → один уточняющий вопрос
  (`_try_fast_path` → `_clarify_for_tool`). Офлайн e2e-тесты:
  `tests/test_offline_deterministic_tools.py` — 7 тестов
  (current_time, system_status, volume, open_app, close_app, list_files +
  clarify при не извлёкшемся аргументе), add_reminder покрыт офлайн-e2e в
  `tests/test_reminders_single_store.py`.
- **R6 — единое хранилище напоминаний** — ВЫПОЛНЕНО (агентом 2, проверено):
  `tests/test_reminders_single_store.py` — 3 теста, включая e2e
  «напомни через 1 минуту выпить воды» при llm_available=False с
  `_FakeClock` → typed `AssistantOutput` + TTS (`test_e2e_reminder_fake_clock_tts_dispatch`).
- **R7 — clarify-цикл** — ВЫПОЛНЕНО.
  Вопрос из слов пользователя (`_extract_object`), обращение из
  профиля (`RoutingContext.addressing` ← `_profile_addressing`),
  TTL 60 c (`_CLARIFY_TTL_SEC`), резолв по топ-2 кандидатам,
  через WS — assistant_output, НЕ confirmation_required
  (`orchestrator.py` ветка clarify). E2E цикла и истечения TTL:
  `tests/routing/test_clarify_cycle_e2e.py` (3 теста, включая обращение
  из профиля и запрет константных обращений). Констант «сэр» в
  semantic_router/safety/agent нет (grep — только комментарии и
  стоп-слово для вырезания из object).
- **R8 — неподдерживаемые запросы** — ВЫПОЛНЕНО.
  `kind=action, tool=None` → `_handle_unsupported` (`core/agent.py`):
  честный отказ без подмены и без вызова провайдера, запись в
  `data/logs/unsupported_requests.jsonl` (redact_secrets, UTC-timestamp,
  топ-3 кандидатов, risk). Тесты: `tests/routing/test_unsupported_requests_logging.py`,
  плюс compound-кейс в `tests/routing/test_compound_split_decision.py`.
- **R9 — warmup до readiness** — ВЫПОЛНЕНО (агентом 3, покрыто тестом):
  `router.warmup()` в `start()` до рукопожатия; `runtime_status.ready`
  только при `router.ready`; провайдер — отдельное поле `provider` по
  health-пробе (`provider_status()`). Транспортный тест:
  `tests/routing/test_ws_route_events.py::test_ws_route_event_contains_full_decision`
  (проверяет ready=True при готовом роутере и наличие поля provider).
- **R10 — trace + WS route event** — ВЫПОЛНЕНО.
  `state["routing_decision"]` (полное решение) + WS-событие `route` с
  {tier, kind, tool, confidence, margin, top3, risk, needs_confirmation,
  latency_ms, llm_available} (`core/ws_server.py`). Тест:
  `tests/routing/test_ws_route_events.py` — 2 теста (создан агентом 4;
  файл отсутствовал и был отмечен «не начато» в шаге 0).
- **R11 — eval integrated vs semantic** — ВЫПОЛНЕНО.
  `scripts/routing_eval.py --router integrated` через реальный
  `Orchestrator.handle_input`. Матрица dev (`artifacts/eval_dev_matrix.txt`):

  | Режим | semantic | integrated | Δ |
  |---|---|---|---|
  | dev, offline | kind 87.50% | kind 87.50% | **0.00 п.п.** |
  | dev, +LLM | kind 92.39% | kind 92.39% | **0.00 п.п.** |

  В обоих режимах и у обоих роутеров: high-risk FN = 0, подмен
  неподдерживаемых = 0, FP на негативах = 0. Блокирующий тест
  `tests/routing/test_routing_eval.py` (dev kind < 85% или FN > 0 → падение)
  зелёный.

---

## 0b. R4 — таблица Tier-0C (оставлено / удалено)

Текущее состояние `SemanticRouter._tier0c_match` + `CANONICAL_SHORTCUTS`
(проверено на диске, semantic_router.py):

| Паттерн (было у агентов 1–3) | Тип | Статус | Куда переехала функция |
|---|---|---|---|
| `mood_complaint` (жалоба о погоде/настроении, exclusion-guard `_T0C_WEATHER_QUERY_RE`) | word-классификатор | **УДАЛЕНО** | chat-якоря + counter-примеры weather |
| `destructive_unsupported` (деструктив + файл-объект) | word-классификатор | **УДАЛЕНО** | kNN-якоря `ANCHORS_ACTION_UNSUPPORTED` (усилены одиночными формами «удали файл/папку») |
| `power_control_unsupported` (+ metaph-guard) | word-классификатор | **УДАЛЕНО** | kNN-якоря `ANCHORS_ACTION_UNSUPPORTED` |
| `reminder_with_time` (напомни + выражение времени) | extraction (время) | **ОСТАВЛЕНО** | `semantic_router.py` `_tier0c_match` п.1 |
| `volume_hint` / `volume_noun_verb` (глагол+объект, negation-guard) | word-классификатор | **УДАЛЕНО** | паспорт `volume` (разговорные формы) + Tier-0A шорткаты «сделай потише/погромче/тише/громче» |
| `volume_with_percent` (громкость + числовой аргумент) | extraction (проценты/числа) | **ОСТАВЛЕНО** | `semantic_router.py` `_tier0c_match` п.2 |
| `play_music_object` (глагол воспроизведения + медиа-объект) | word-классификатор | **УДАЛЕНО** | паспорт `play_music` |
| `close_named_app` (глагол закрытия + имя приложения) | word-классификатор | **УДАЛЕНО** | паспорт `close_app` |
| `unsupervised_mission` («пока меня нет») | word-классификатор | **УДАЛЕНО** | kNN-якоря `ANCHORS_MISSION` |

Итог: в Tier-0C осталось 2 extraction-паттерна (время, проценты/числа);
Tier-0A — 35 точных полнофразовых шорткатов; разговорные формы — в
`capability_examples_ru.py`. `tests/routing/test_no_eval_leak.py` — зелёный.

---

## 2. Матрица соответствия требованиям (R1–R12)

| Требование | Описание | Статус | Proof-line |
|---|---|---|---|
| **R1** | `route()` — единственное решение kind/tool; нет параллельных классификаторов | Выполнено | `core/orchestrator.py:970`, `core/agent.py:791`; `world.router.route` удалён |
| **R2** | 20 keyword-гейтов удалены или тонкие адаптеры с DEPRECATED | Выполнено | Раздел 3 ниже: таблица 20 позиций по grep живых вызовов (state.py и маркер «неизвестная команда» доликвидированы агентом 4) |
| **R3** | `safety.py` — адаптер над `assess_risk`; эскалация по аргументам | Выполнено | `core/safety.py:96-120`, `core/routing/semantic_router.py:356-375`, `tests/routing/test_risk_escalation_by_arguments.py` (11 passed) |
| **R4** | Tier-0C extraction-only; разговорные формы в паспортах; no eval leak | Выполнено | Раздел 0b ниже (таблица оставлено/удалено); `tests/routing/test_no_eval_leak.py` PASSED |
| **R5** | Детерминированные действия без LLM (`llm_available=False`, `verified=True`) | Выполнено | `core/agent.py:_try_fast_path`, `_DETERMINISTIC_TOOLS`; `tests/test_offline_deterministic_tools.py` (7 passed) |
| **R6** | Напоминания: одно хранилище (`TaskRuntime`), typed `AssistantOutput` + TTS | Выполнено | `tests/test_reminders_single_store.py` (3 passed, вкл. e2e с `_FakeClock`) |
| **R7** | Clarify: вопрос из слов пользователя, обращение из профиля, TTL 60с | Выполнено | `core/orchestrator.py:455-556`; `tests/routing/test_clarify_cycle_e2e.py` (3 passed) |
| **R8** | `kind=action, tool=None` -> честный отказ + запись в `unsupported_requests.jsonl` | Выполнено | `core/agent.py:_handle_unsupported`; `tests/routing/test_unsupported_requests_logging.py` + compound-кейс |
| **R9** | `router.warmup()` до readiness; `runtime_status.ready` только с готовым индексом | Выполнено | `core/orchestrator.py:_warmup_router` в `start()`; `core/ws_server.py:280-325`; `tests/routing/test_ws_route_events.py` |
| **R10** | trace и WS-событие `route` с полной телеметрией | Выполнено | `core/ws_server.py:868-895` (10 полей decision); `tests/routing/test_ws_route_events.py` (2 passed) |
| **R11** | `scripts/routing_eval.py` совпадает с semantic в пределах 2 п.п. | Выполнено | dev: semantic 87.50%/92.39% == integrated (Δ 0.00 п.п., оба llm-режима); блокирующий `tests/routing/test_routing_eval.py` зелёный |
| **R12** | Полный pytest: 0 failed, passed >= 901 | Выполнено (подтверждено финальным прогоном) | `artifacts/full_run_step1b.txt`: 946 passed, 3 skipped, 0 failed |

---

## 3. R2 — финальная таблица 20 позиций инвентаря (grep живых вызовов, 2026-09-05)

Проверка по grep по `core/ tests/ scripts/` (без `__pycache__`), а не по памяти.

| № | Позиция | Статус по grep | Основание |
|---|---|---|---|
| 1 | `_MEDIA_ACTION_MARKERS`, `_BROWSER_ACTION_MARKERS` | **УДАЛЕНО** | Совпадения только в `intent_router.py` (объявления, мёртвый код) — живых вызовов нет |
| 2 | `_CATEGORY_KEYWORDS`/`resolve_keyword_tool` как гейт | **УДАЛЕНО КАК ГЕЙТ** | Из живого пути убраны: `core/state.py new_state` (агент 4), оркестратор проставляет intent из `semantic_intent_category(decision)` (`orchestrator.py:582`). Остаточные вызовы — информационные: `route_guard.validate_tool_selection` (intent не влияет на `allowed`), `understanding/layer` (route перекрывается decision в `orchestrator.py:975-984,1061-1070`), `council` (вне контура запросов), eval-baseline |
| 3 | `split_compound_commands` | **АДАПТЕР** | Только синтетический генератор кандидатов внутри `split_compound_by_decision` (`semantic_router.py`); разбиение валидируется решением роутера по каждой части |
| 4 | `_TRIVIAL_RE`, `_SIMPLE_COMMAND_RE` | **ОСТАВЛЕНО с причиной** | `model_router.py` — оценка сложности для ВЫБОРА ТИРА модели (LLM backend), kind/tool не решают |
| 5 | `_REASONING_RE`, `_CODE_RE`, `_ARCH_RE`, `_PRIVATE_RE` | **ОСТАВЛЕНО с причиной** | Там же: признак сложности генерации для тира модели, не маршрутизация запроса |
| 6 | `_ACTION_VERB_RE`, `_DOMAIN_NOUN_RE`, `_CONVERSATION_HINT_RE` | **УДАЛЕНО ИЗ ЖИВОГО ПУТИ** | `classify_conversation` живых вызовов не имеет (только tests + eval-baseline); conversation gate в агенте читает `decision.kind` (`agent.py:1031-1037`) |
| 7 | Hard conversation gate (`intent none + len<=8`) | **УДАЛЕНО ИЗ ЖИВОГО ПУТИ** | Находится внутри мёртвой `classify_conversation` (`model_router.py:299`); из контура запросов не вызывается |
| 8 | `_try_world_perception` / `world.router.route` | **УДАЛЕНО** | grep пуст (функция удалена агентом 3) |
| 9 | `_try_fresh_information` keyword-маркеры | **УДАЛЕНО** | grep маркеров «погод/курс доллар» в agent.py пуст; fresh-путь читает `decision.kind == "fresh_data"` (`agent.py:1857-1862`) |
| 10 | Первичный `classify_conversation` в Agent | **УДАЛЕНО** | В агенте только комментарий (`agent.py:1031`); гейт `decision.kind in ("chat","question") and tool is None` |
| 11 | Маркеры «неизвестная команда» | **УДАЛЕНО** | Substring-гейт удалён агентом 4 (`agent.py`, блок после ingest); `_handle_unknown` остался для planning/discovery-ошибок |
| 12 | `_match_skill` (Skill Forge) | **ОСТАВЛЕНО с причиной** | Runtime-загрузка пользовательских расширений (`agent.py:996`); матчинг по зарегистрированным навыкам, не классификатор текста |
| 13 | `_try_fast_path` intent-гейт | **УДАЛЕНО** | Отбор по `decision.tool` и `_DETERMINISTIC_TOOLS` (`agent.py:1774-1815`) |
| 14 | `_extract_simple_args` | **АДАПТЕР** | Структурное извлечение аргументов для уже выбранного роутером инструмента (`agent.py:1894+`) |
| 15 | Вторичный `classify_conversation` перед planner | **УДАЛЕНО** | См. №6/№10 — гейт читает decision |
| 16 | `_CRITICAL/_HIGH/_MEDIUM_RISK_PATTERNS` в safety | **УДАЛЕНО** | `core/safety.py:45-50` — комментарий о удалении; адаптер над `semantic_router.assess_risk` |
| 17 | `_RULES` в `UniversalIntake.classify` | **ОСТАВЛЕНО с причиной** | Внешний контур разметки TaskContract (метаданные миссии), маршрут не определяет (`intelligence/intake.py:26`) |
| 18 | `_keyword_score` в CapabilityRegistry | **ОСТАВЛЕНО с причиной** | Вторичный канал гибридного скоринга внутри discovery-каталога (`capabilities.py:601,657`), после решения роутера |
| 19 | `UnderstandingLayer._classify` | **ПЕРЕКРЫТО decision (не гейт)** | Regex-маркеры считаются, но `route` полностью подменяется решением роутера (`orchestrator.py:975-984, 1061-1070`); наружу используется только `understanding.confidence` как метаданные |
| 20 | `QUICK_MEMORY_MARKERS`, `_INSTANT_KNOW` | **ОСТАВЛЕНО с причиной** | Выбор ИСТОЧНИКА ответа (память/поиск) внутри question-пути, куда попадают только `decision.kind == "question"`; маршрут определяет роутер (`understanding/quick_answer.py:38-45`) |
