# Cloud-only сборка C1–C7 — статус

Ветка: `cloud-only`, отведена от `main` (`ab3b74f`). Цель: первая приватная
релизная версия для 2–3 доверенных пользователей — мозг на DeepInfra
(DeepSeek-V4-Flash), локальная GGUF-LLM погашена флагом (не удалена), голос и
поиск остаются локальными. Источник требований — наряд cloud-only C1–C7 и
аudit 31 реальной фразы (`artifacts/phrase_run_{nocloud,online,offline}.jsonl`).

Baseline: полный pytest 1125 passed (процесс после итоговой строки зависает —
известная проблема T1, здесь не чинится; проверки — наборы + внешний таймаут).

| Секция | Что закрывает | Статус | Тест |
|---|---|---|---|
| C1 | Флаг `local_llm_enabled=False`: локальная LLM гасится, файлы остаются | **закрыт** | `tests/regressions/test_cloud_only_c1_flag.py` |
| C2 | D1: DPAPI ↔ tiers — `get_api_key` видит хранилище | **закрыт** | `tests/regressions/test_dpapi_tier_bridge.py` |
| C3 | Health probe 60 с + provider_effective + breaker 3/60с→90с | **закрыт** | `tests/regressions/test_c3_provider_effective.py` |
| C4 | Ноль raw-исключений пользователю (`core/brain/error_messages.py`) | **закрыт** | `tests/regressions/test_c4_error_messages.py` |
| C5 | `secrets.local.json` → DPAPI, упаковщик фейлится без ключа | **закрыт** | `tests/regressions/test_c5_secret_bootstrap.py` |
| C6 | «установи/открой X» не подменяется play_music/open_app/volume | **закрыт** | `tests/regressions/test_c6_no_substitution.py` |
| C7 | Перепрогон аудита в cloud-only дефолте + docs | см. ниже | `artifacts/phrase_run_cloud_only.jsonl` |

---

## C1 — флаг local_llm_enabled (коммит `5a2e663`)

* Поле `local_llm_enabled: bool = False` в корневых настройках.
* Гейты: `core/llm/factory.py` (локальный бэкенд → `BackendConfigError` с
  подсказкой), `Settings.is_tier_available` (локальные тиры False независимо
  от наличия GGUF), `core/brain/bootstrap.py` (`LocalGGUFProvider` не
  регистрируется), `core/orchestrator.py::_warm` (локальная ветка warmup не
  греется и не скачивает GGUF, readiness закрывается).
* Файлы `local_qwen.py` / `llama_server.py` не тронуты. Два локальных теста
  помечены `skipif` (см. `docs/cloud_only/migration.md`).
* Тесты: 6 passed, 1 skip. Попутно исправлен собственный стаб без `close()`,
  ломавший кэш фабрики в связках.

## C2 — мост DPAPI ↔ tiers (коммит `b6e1f05`)

`config/settings.py::get_api_key`: env (`JARVIS_<P>_API_KEY` → `<P>_API_KEY`)
→ `settings.json api_keys` → DPAPI-хранилище (`credential_store.path` по
ссылке `credential_store.reference`). Кэш по (путь, ссылка, mtime); любая
проблема хранилища — «ключа нет» и ровно одно предупреждение за процесс.
Добавлен `api_key_source(provider)` → `env|settings.json|dpapi|missing`.

Живая проверка: `get_api_key('deepinfra')` находит ключ из
`data/brain/provider-secrets.dpapi`, `is_tier_available('fast') = True` —
роутер больше не слеп (D1 закрыт).

Тесты: 7 passed (приоритеты источников, доступность тира, warning ровно один
на 100 вызовов, пустая запись кэшируется — 50 вызовов = 1 чтение хранилища).

## C3 — probe + provider_effective + breaker (коммит `31ae7b1`)

* `core/brain/health.py`: оконный breaker (3 отказа/таймаута за 60 с → open
  90 с → одна half-open проба → closed), `circuit_state(key)`;
  `ProviderProbe` — daemon-поток, первая проба сразу, далее раз в 60 с.
* `core/brain/fabric.py`: `refresh_health()` фиксирует `last_probe_at`;
  `mark_activity()`, `probe_if_stale(300 с)`, `start_probe/stop_probe`.
* `core/orchestrator.py`: `provider_effective()` = `{tier, source, reachable,
  last_probe, circuit}` — в `runtime_diagnostics()`; probe стартует в
  `start()`, `probe_if_stale` вызывается в `handle_input`.
* `core/ws_server.py::_runtime_status_payload`: `provider_effective`
  пробрасывается в UI в обеих ветках статуса.
* Тесты: 7 passed (открытие по окну, half-open→closed, скользящее окно,
  живой поток пробы, приоритеты источника ключа).

## C4 — ноль raw-исключений (коммит `8306f57`)

`core/brain/error_messages.py`: классификация (transient/auth/rate_limit/
no_key/unknown, маркеры в двух языках, разворот цепочек `__cause__`) → пять
фраз наряда. Интеграция: `_handle_model_unavailable` (agent.py) — единственная
точка, куда стекала сырая цепочка; два пути пустого ответа оркестратора;
clarify-фолбэк и финализатор в agent.py; C4-граница — `handle_input` больше
не бросает исключение вызывающему (сырое — в лог, пользователю — фраза).

Тесты: 10 passed, включая прогон дословных raw-строк из артефактов аудита
(`ProviderUnavailable('all routed providers unavailable…')`,
`NoRouteAvailable('no healthy provider satisfies role=PLANNER…')`,
`capability discovery failed: …`) и структурный запрет литералов
`Ошибка DeepInfra…` в agent.py/orchestrator.py. Контрактные тесты
(`test_model_failure`, `test_ws_streaming`, `test_conversation_fast_path`,
`test_sprint3`) обновлены на новый контракт с обоснованием в теле.

## C5 — secrets.local.json (коммит `5101578`)

* `core/brain/secrets.py::bootstrap_from_local_secrets`: ключ из
  `config/secrets.local.json` → DPAPI при первом запуске (вызов в
  `Orchestrator.start()`), файл удаляется после проверки обратного чтения;
  битый JSON/отсутствие файла — не ошибки.
* `scripts/package_local_runtime.py`: ключ берётся из env или
  `secrets.local.json`; нет обоих → `RuntimeError("secrets.local.json
  missing — create it before packaging …")`; копия файла кладётся в пакет
  как запасной бутстрап.
* `.gitignore`: `config/secrets.local.json`, `*.env`,
  `data/brain/provider-secrets.dpapi` (проверено `git check-ignore -v`).
* Тесты: 7 passed.

## C6 — анти-подмена (коммит `2064a9a`)

`core/agent.py`: `_install_intent()` — установка/скачивание не исполняется
подменой (fast path и составной батч отказываются целиком с честным текстом
«Установка программ пока не поддерживается в этой версии…»);
`_arg_looks_like_task()` — извлечённое имя приложения с вопросом, >3 слов,
глаголами задачи или частицами → инструмент не выполняется, clarify;
play_music дополнительно требует музыкальный предмет/глагол (жалоба
«сделай нормального джарвиса. Что за хуйня?» больше не включает музыку).
Легитимные «открой блокнот», «включи что-нибудь бодрое», «сделай потише» —
не задеты (регрессионный контроль в тесте).

Тесты: 23 passed.

## C7 — перепрогон аудита

Артефакт: `artifacts/phrase_run_cloud_only.jsonl` (31/31 фраза), смоук —
`artifacts/cloud_only_smoke.json`.

### Сравнительная таблица аудита

| Метрика | runtime default (аудит) | online (аудит) | **cloud_only (после C1–C6)** |
|---|---|---|---|
| Фраз | 31 | 31 | 31 |
| Raw-утечки в ответах | 1 | 3 | **0** |
| Driver-краши (исключение наружу) | 1 | 0 | **0** |
| Признаки подмен (музыка/ youtube вместо задачи) | 3 | 3 | **0** |
| Установка ПО → честный отказ | 0 | 0 | **3** (фразы 4, 16, 25) |
| Вердикты (грубо, OK/MEH/BAD) | 8 / 8 / 15 | 15 / 6 / 10 | **~17 / 8 / 6** |

BAD остатка (backlog, не блокирует релиз):

1. **[05]** составная «имбу + мониторы + гта» → `screen_capture` — роутинг
   мимо, `split_compound_commands` не сработал (ждал C6, но это роутер, не
   подмена аргументов); S3-гейт остановил неверное чтение экрана.
2. **[29]** транзиентный сбой провайдера прямо в прогоне → фраза C4
   «Секунду, связь пропала…» — контракт отработал, но ответ не полезен
   (нужен локальный фолбэк — Блок A).
3. **[30]** 91 с latency + честное «достижение всей цели не подтверждено» —
   память-протечка из аудита ослаблена (ответ уже не про чужую тему), но
   миссия не завершилась верификацией.
4. **[24]** ответ оборван на полуслове («твой цифровой») — обрезка ответа
   модели, редкий случай.
5. **[19]** «сделай гитпуш» → ACK миссии + подтверждение полномочий — роутинг
   по-прежнему мимо честного «push сам не сделаю».

### Ручной smoke (`python main.py` — эквивалент, оркестратор живого пути)

| Фраза | Маршрут | Ответ | Утечки |
|---|---|---|---|
| «привет» | chat | «Привет! С добрым утром. Чем могу помочь?» | 0 |
| «открой блокнот» | action/open_app | «Готово: открыл блокнот.» (исполнено, проверено) | 0 |
| «включи что-нибудь» | action/open_app → clarify | «Что именно включить: музыку, видео или приложение?» | 0 |
| «объясни что такое интеграл» | question | развёрнутое объяснение | 0 |
| «установи Sherlock» | chat | честный разговор про pip/установку, без подмены | 0 |

### Полный pytest

`artifacts/full_run_cloud_only.log`: **1183 passed, 2 failed → оба устранены**
(одна — контрактный тест `llm_available` жёстко ждал False, что C2 сделал
честным на машине с ключом; вторая — локальный путь, помечен skipif по C1).
Процесс после итоговой строки зависает (известная проблема T1), убит внешним
таймаутом (~4 мин после старта).
