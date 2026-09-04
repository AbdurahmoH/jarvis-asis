# APP: полный сравнительный анализ двух проектов

> Дата среза: 2026-09-04  
> Рабочая область: E:\jarvis-2.4-main  
> Формат: архитектурный аудит, инвентаризация кода, проверка сборки и тестов.

## 1. Итоговый вердикт

В рабочей области находятся не две независимые версии ассистента, а один продукт, разделённый на два исходных проекта:

1. **Backend / Python core** — корень E:\jarvis-2.4-main.
2. **Desktop UI / Tauri + React** — E:\jarvis-2.4-main\jarvis.

Backend содержит основную функциональность: оркестрацию, агентный цикл, модели, память, инструменты, голос, компьютерное управление, верификацию и фоновые миссии. Frontend — рабочая оболочка рабочего стола, которая подключается к Python core через WebSocket и запускает его через Tauri.

Система уже выглядит как функциональный alpha/early-production стек, а не как макет: найдено 26 зарегистрированных инструментов, 207 Python-файлов в core, около 69 тыс. строк Python-кода с тестами и скриптами, 72 ключевых frontend/config-файла и около 7 тыс. строк TypeScript/Rust/CSS. Frontend production-сборка и его контрактные тесты проходят.

Главный релизный риск — не отсутствие функций, а рассинхрон между исходниками, документацией, версией и собранным runtime-пакетом. Полный Python-тестовый прогон сейчас завершается с 5 падениями при 814 успешных тестах. До публикации сборки нужно сначала синхронизировать staged runtime и исправить продуктовые контракты.

**Коротко:** архитектурная база сильная, но канонический путь запуска, версия, runtime-артефакты и несколько интеграционных контрактов ещё не сведены в одну чистую линию.

---

## 2. Область анализа и границы

### 2.1. Проект A — Python backend

**Путь:** E:\jarvis-2.4-main

В область вошли:

- main.py, config, core, hud, persona;
- scripts сборки, запуска, smoke-проверок и упаковки;
- tests;
- data как runtime-состояние и хранилище, без чтения содержимого секретов;
- docs и README для сопоставления заявленной и фактической архитектуры;
- pyproject.toml, requirements.txt, конфигурационные файлы.

### 2.2. Проект B — desktop frontend

**Путь:** E:\jarvis-2.4-main\jarvis

В область вошли:

- src — React/TypeScript UI, stores, hooks, protocol и WebSocket transport;
- src-tauri/src — Rust launcher, tray, окна и Windows-интеграция;
- src-tauri/tauri.conf.json, capabilities, Cargo.toml;
- package.json, package-lock.json, Vite/TypeScript-конфигурация;
- frontend scripts и тесты;
- src-tauri/resources/jarvis-runtime — отдельно как staged/package artifact, не как третий исходный проект.

### 2.3. Что не считается третьей версией

Внутри frontend есть src-tauri/resources/jarvis-runtime. Это не самостоятельная версия исходников, а подготовленная копия payload для упаковки. Она сейчас содержит собственный core и расходится с текущим корнем, поэтому её следует считать **снимком сборки**, а не третьим проектом.

Соседние каталоги E:\jarvis-project и E:\jarvis-github-export-20260818 не содержат полноценного исходного проекта: в них найден только пустой служебный файл NUL. Поэтому сравнительный отчёт построен по двум реально наполненным папкам выше.

---

## 3. Количественный срез

| Область | Измерение | Результат |
|---|---:|---:|
| Python backend | Python-файлы в основном дереве, включая scripts/tests | 359 |
| Python backend | Строки в этом срезе | 68 957 |
| core | Python-модули | 207 |
| Frontend | src, src-tauri/src, public и конфиги, без node_modules/target | 72 файла |
| Frontend | Строки TypeScript/Rust/CSS в этом срезе | 7 056 |
| Backend tools | Зарегистрированные Tool | 26 |
| Backend verifiers | Инструменты со strict-верификацией | 21 |
| Frontend build | Vite production build | PASS |
| Frontend tests | Три контрактных набора | PASS |
| Python tests | Полный прогон | 814 passed, 5 failed, 2 skipped |
| Python syntax | compileall для основного backend | PASS |

Самые крупные backend-файлы:

- core/agent.py — 3 805 строк;
- core/task_runtime.py — 1 540 строк;
- core/orchestrator.py — 1 478 строк;
- core/ws_server.py — 1 274 строки;
- core/platform/browser_bridge.py — 1 013 строк;
- core/capability_engine.py — 781 строк;
- core/executive/world.py — 721 строка;
- core/llm/remote_api.py — 714 строк;
- core/capabilities.py — 705 строк;
- core/verifier.py — 667 строк.

Самые крупные frontend-файлы:

- jarvis/src-tauri/src/main.rs — 364 строки;
- jarvis/src/integrations/wsBackend.ts — 360;
- jarvis/src/components/SettingsPanel.tsx — 299;
- jarvis/src/styles/presence.css — 278;
- jarvis/src/integrations/backend.ts — 265;
- jarvis/src/components/Sidebar.tsx — 253;
- jarvis/src/App.tsx — 241;
- jarvis/src/stores/sessionStore.tsx — 229;
- jarvis/src/types/index.ts — 221.

---

## 4. Системная архитектура

~~~mermaid
flowchart LR
    UI["Tauri + React UI"] --> WS["WebSocket protocol"]
    WS --> ORCH["Orchestrator"]
    ORCH --> UNDER["Understanding / Quick Answer"]
    ORCH --> AGENT["Agent execution loop"]
    ORCH --> TASK["TaskRuntime missions"]
    AGENT --> ROUTER["BrainFabric / ModelRouter"]
    AGENT --> CAPS["CapabilityCatalog / Tools"]
    AGENT --> VERIFY["Verifier + RepairLoop"]
    AGENT --> MEM["Graph / Chroma / session memory"]
    AGENT --> EXEC["ExecutiveMind"]
    ORCH --> VOICE["STT / Piper TTS / Wake word"]
    ORCH --> PROACTIVE["LivingIntelligence / Shadow / Scheduler"]
    CAPS --> OS["Windows / browser / filesystem / web / media"]
    TASK --> DATA["Durable mission JSON"]
    MEM --> DATA2["SQLite / Chroma / JSON state"]
~~~

### 4.1. Слои системы

| Слой | Ответственность | Ключевые файлы |
|---|---|---|
| Entry/bootstrap | запуск, логирование, signal handlers, CLI | main.py, core/ws_server.py |
| Transport | WebSocket, JSON envelopes, reconnect, confirmations | core/ws_server.py, jarvis/src/integrations/wsBackend.ts, wsProtocol.ts |
| Input understanding | классификация команд, вопросов, миссий и контекста | core/understanding/*, core/agent.py, core/orchestrator.py |
| Orchestration | сборка сервисов и выбор synchronous/background пути | core/orchestrator.py |
| Agent loop | intent → risk → context → plan → tools → verification → repair → memory | core/agent.py |
| Model layer | роли мозгов, provider routing, health, fallback, streaming | core/brain/*, core/llm/* |
| Capability layer | единый каталог инструментов и metadata | core/actions/*, core/capabilities.py |
| Execution | Windows, browser, filesystem, web, media, reminders | core/actions/*, core/platform/*, core/operator/* |
| Risk/authority | risk score, confirmation, grants, effect classification | core/router/route_guard.py, core/authority/* |
| Verification | strict checks, evidence, repair attempts | core/verifier.py, core/repair/*, core/metacognition/* |
| Executive layer | goals, commitments, world model, command compilation | core/executive/* |
| Memory | session, graph, vector, profile, relationship, document RAG | core/memory/* |
| Proactive layer | background decisions, triggers, shadow rehearsals | core/living/*, core/proactive/*, core/shadow/* |
| Voice/vision | STT, TTS, wake word, screenshots/OCR | core/voice/*, core/vision/* |
| Persistence/security | atomic files, redaction, DPAPI credential reference | core/security/*, data/* |
| Desktop shell | windows, tray, autostart, child process, vibrancy | jarvis/src-tauri/src/* |

---

## 5. Backend: точный разбор

### 5.1. Точки входа

#### main.py

Console entrypoint делает следующее:

1. загружает settings;
2. включает logging;
3. применяет hardware profile;
4. создаёт необходимые каталоги;
5. создаёт Orchestrator;
6. устанавливает обработчики завершения;
7. запускает orchestrator;
8. обслуживает REPL-команды.

Поддерживаются команды выхода, паузы/возобновления, status, models, добавления модели, голосовой проверки и confirm/reject. Обычный пользовательский текст уходит в orchestrator.handle_input.

README правильно указывает, что основной desktop-запуск идёт через launcher/GUI, а не через ручной запуск main.py.

#### core/ws_server.py

Это production transport layer для frontend:

- loopback WebSocket server;
- JSON protocol;
- отдельный event loop и server thread;
- runtime readiness;
- command, voice, settings, confirmation, interrupt и screen capture messages;
- event broadcasting для state, mission, token streaming, actions, tools, progress, result и diagnostics;
- per-connection rate limiting;
- origin allowlist в основном run_server пути;
- optional token authentication через environment.

WebSocket transport не должен содержать бизнес-логику: он правильно делегирует команды в orchestrator/task runtime и переводит внутренние события в wire envelopes.

### 5.2. Orchestrator

core/orchestrator.py — composition root и главный координатор. Он собирает:

- BrainFabric;
- ModelRouter и legacy compatibility paths;
- CouncilRouter;
- MemoryRetriever;
- CognitiveKernel mission ledger;
- UnderstandingLayer и QuickAnswerEngine;
- Agent;
- ShadowEngine;
- TaskRuntime;
- AuthorityStore;
- LivingIntelligence;
- CognitiveOrchestrator;
- Piper TTS, SpeechRenderer, TTSQueue;
- TaskManager, Proactor, BackgroundScheduler;
- universal intake и tutor.

handle_input — фактический главный путь пользовательского запроса:

1. стартует runtime при необходимости;
2. добавляет activity/taste context;
3. делает understanding pass;
4. обрабатывает scheduled/conditional/delegated/control/follow-up modes;
5. быстрые reflex actions и контекстные ответы возвращает напрямую;
6. вопросы пропускает через quick-answer path;
7. сложные миссии отправляет в TaskRuntime и сразу возвращает ACK с mission id;
8. обычные synchronous команды отправляет в Agent.execute;
9. возвращает typed output с response, tool, verified, route, mode, latency и evidence.

Для длинных задач _mission_runner связывает Agent.run_mission с mission ledger, TaskRuntime events, memory и TTS.

### 5.3. Агентный цикл core/agent.py

Заявленный и фактически реализованный цикл:

~~~text
USER GOAL
  → INTENT
  → RISK
  → CONTEXT
  → MODE
  → PLAN
  → TOOL RETRIEVAL
  → EXECUTION
  → VERIFICATION
  → REPAIR
  → RESULT
  → MEMORY / SKILL
~~~

Основные ветки:

- perception для вопросов о текущем состоянии;
- fresh public information для currency/news/weather/price/version;
- conversation gate до действия;
- CommandOS compile для executive commands;
- compound request split в verified batch;
- large-input ingestion;
- explicit unknown-capability research;
- skill matching/forging;
- model route;
- deterministic fast path для app/system/media/web;
- graph/executive memory retrieval;
- capability discovery и structured plan;
- route guard и risk gate;
- confirmation state для high-risk действий;
- tool execution с retry;
- strict verification;
- deterministic failure handling, research-pending или repair loop;
- verified fact persistence и naturalized final response.

AgentConfig ограничивает retrieval, plan size, repair count, action iterations и confirmation timeout. Это хороший баланс: модель не получает бесконечный цикл, а execution budget задан явно.

### 5.4. TaskRuntime

core/task_runtime.py — durable runtime для долгих и отложенных миссий.

#### Состояния

pending, waiting, triggered, queued, acknowledging, analyzing, planning, executing, verifying, repairing, completed, paused, cancelled, failed, expired.

#### Модель миссии

Хранит:

- goal и context;
- progress/current step;
- model и tools;
- result/error;
- verification;
- trigger;
- attempt state;
- completion criteria;
- expiry;
- plan;
- event history;
- evidence.

#### Сильные решения

- thread-safe EventBus;
- ограничение active concurrency;
- durable JSON persistence;
- atomic writes;
- restore после перезапуска;
- recovery active missions через PAUSED;
- cancellation/pause/resume/skip step;
- watchdog hook;
- runner abstraction без хранения executable code в mission payload.

#### Риски

Mission.events и _missions растут без видимого retention policy. При большом числе фоновых задач это создаёт неограниченный рост памяти и JSON. В data/executive уже обнаружены временные файлы атомарной записи большого размера рядом с goals.json. Нужны compaction, event retention, size caps и уборка orphan temp files.

### 5.5. Brain/model routing

В системе одновременно живут два слоя маршрутизации.

#### Новый слой: core/brain

Типизированные сущности:

- BrainRole: CHAT, FAST, REASONING, CODER, PLANNER, RESEARCH, VISION, CRITIC, SUMMARIZER, FALLBACK;
- PrivacyClass: PUBLIC, PERSONAL, SENSITIVE, LOCAL_ONLY;
- BrainRequest, BrainRoute, BrainResult;
- provider config и health state;
- circuit breaker и fallback policy.

SemanticBrainRouter оценивает кандидатов по role, context, cost, locality, quality, speed и degraded state. BrainFabric умеет generate/stream, отслеживает failures и применяет redaction/policy gate.

#### Legacy/compatibility слой: core/llm

ModelRouter классифицирует complexity по reasoning/code/architecture/private/multistep/size и историческим tiers FAST → ANALYST → CODER → ARCHITECT. Он нужен для совместимости, но в текущем DeepSeek-mode production path главным становится BrainFabric.

#### Фактический текущий режим

config/settings.json задаёт:

- primary_brain = fast;
- offline_mode = false;
- deepseek_brain_mode = true;
- один cloud provider;
- один remote model string;
- max_fallbacks = 0;
- prefer_local = false;
- allow_cloud = true;
- local GGUF settings остаются в файле, но не являются активным production brain path.

Это важное поведение: при сбое единственного cloud provider система не имеет автоматического local fallback. Для latency и предсказуемости это понятно, но для availability это single-provider failure domain.

### 5.6. Каталог инструментов

core/actions использует единый Tool, ToolContext, ActionResult и ToolRegistry. core/capabilities.py добавляет metadata: описание, примеры, risk, permissions, requirements, speed, cost, internet/file access, success check, fallbacks, tags и evidence scope.

Каталог из 26 зарегистрированных tools:

| Группа | Инструменты |
|---|---|
| Apps/system | open_app, close_app, system_status, volume, current_time |
| Web/data | web_search, web_fetch, weather, public_data |
| Files | list_files, read_file, write_file, search_files, file_copy, file_move, list_files_recursive |
| Reminders | add_reminder, list_reminders, cancel_reminder |
| Capture/browser | screen_capture, browser_automation, browser_bridge |
| Computer input | computer_mouse, computer_keyboard, computer_screenshot |
| Media | play_music |

Strict verifiers зарегистрированы для 21 инструментов. High-risk computer/browser tools требуют risk gate/confirmation. browser_automation имеет внутреннее evidence scope, а user-visible bridge размечен отдельно — это правильное разделение внутреннего execution trace и UI evidence.

Capability retrieval гибридный: keyword score + embedding score. Embedding model загружается лениво, при недоступности работает keyword fallback. Для model-led discovery сначала отдаётся summary surface, schema раскрывается после выбора.

### 5.7. Verification, repair и authority

После tool call агент не принимает raw output за факт. Он строит ActionResult, запускает verifier, прикладывает evidence и только после успешной проверки формирует verified response.

Ветка failure:

1. deterministic policy failure;
2. research pending для web/data, если источник не дал результата;
3. RepairLoop с ограниченным числом попыток;
4. повторная проверка risk/confirmation;
5. backlog, если repair не помог.

core/authority разделяет effect classification, structured grant и delegated authority. Это важнее простого boolean confirmed: grant можно привязать к миссии, действию и сроку.

### 5.8. Memory и durable state

В проекте несколько специализированных видов памяти:

- session/short-term conversation;
- SQLite graph memory;
- Chroma long-term vector memory;
- document RAG;
- profile/relationship/taste memory;
- executive goals, commitments и world state;
- cognitive evidence;
- shadow backlog;
- mission state;
- metacognitive calibration/failure/belief stores.

Секретный фильтр убирает passwords, tokens, keys и raw tracebacks из durable memory. Credential store использует Windows DPAPI reference file и masked settings API. Содержимое секретных файлов в ходе анализа не читалось и в отчёт не включается.

### 5.9. Voice, vision и proactive layers

#### Voice

- Piper TTS;
- TTSQueue;
- speech renderer и sanitizers;
- faster-whisper STT;
- wake-word layer;
- barge-in через hotkey;
- typed AssistantOutput между WS и TTS.

#### Vision/computer use

- screenshot capture;
- OCR/grounding;
- geometry/reflection;
- browser semantic controls;
- mouse/keyboard actions;
- verification after UI action.

#### Proactive

- background scheduler;
- system triggers;
- LivingIntelligence context sampler;
- friction/goal tracker;
- workflow resources;
- shadow rehearsal/backlog;
- sleep mode и temporal memory.

Такой набор превращает продукт из chat UI в desktop agent, но одновременно повышает требования к lifecycle, privacy, retention и end-to-end testing.

---

## 6. Frontend: полный разбор

### 6.1. Стек и product shell

jarvis/package.json задаёт:

- React 18.3;
- TypeScript 5.6;
- Vite 5.4;
- Tauri 2;
- Framer Motion;
- Lucide React;
- date-fns;
- Tauri plugins для shell, opener, dialog, fs, http, notification, process и global shortcut.

UI построен как frameless transparent desktop shell с несколькими режимами:

- основной operator shell;
- overlay для быстрого ввода;
- tray icon;
- sidebar/context drawer;
- activity stream;
- command composer;
- confirmation card;
- settings;
- mission/operator panel;
- vitals/runtime diagnostics;
- visual fixture/preview mode.

### 6.2. App.tsx

App — текущая production composition point frontend.

Он:

1. определяет overlay window;
2. включает fixture только при явном dev/query flag;
3. создаёт WebSocketBackend для HOST:PORT loopback endpoint;
4. подключает StateMachine и TTSController;
5. подписывается на backend events;
6. собирает messages, mission, signals, confirmations, runtime diagnostics;
7. обрабатывает streaming start/token/end;
8. управляет first launch и mode;
9. шлёт command/confirm/interrupt/voice;
10. рендерит TrayIcon, OperatorShell или InputOverlay.

Важно: production App уже не полагается на заранее объявленный mock-ready state. Connected/runtime readiness приходят от transport.

### 6.3. Состояния UI

Frontend разделяет:

- entity state: idle, thinking, speaking, executing, error;
- connection state;
- runtime state: starting, loading_model, ready, unavailable;
- mission state и event rail;
- pending confirmation;
- messages/streaming bubble;
- signals/tool progress;
- mode/theme/settings.

reduceMission и typed BackendEvent дают локальный reducer-подход для timeline. Это лучше, чем держать один большой mutable объект.

### 6.4. WebSocket transport

jarvis/src/integrations/wsBackend.ts реализует:

- connect/reconnect;
- exponential backoff 250 ms → 5 s плюс jitter;
- connection listeners;
- command dispatch;
- confirmation;
- interrupt;
- voice/hotkey;
- first launch;
- ambient events;
- settings get/save;
- vitals cache;
- system error events.

wsProtocol.ts — boundary mapper. Он разбирает state/event/confirmation/vitals/profile/runtime_status/voice_input/screen_capture/error и игнорирует неизвестные envelopes. Это правильная защита UI от произвольного payload, хотя часть downstream casts всё ещё остаётся небезопасной для TypeScript.

### 6.5. Tauri/Rust launcher

jarvis/src-tauri/src/main.rs:

- находит project/runtime root;
- читает launcher settings;
- разрешает абсолютный путь backend process;
- задаёт JARVIS_HOME, encoding и runtime temp variables;
- запускает дочерний backend без консольного окна Windows;
- мониторит child process;
- перезапускает его при падении/not-listening;
- создаёт main/overlay windows;
- добавляет tray menu;
- регистрирует Ctrl+Space;
- поддерживает autostart через registry;
- открывает settings/debug/quit actions;
- корректно останавливает child при завершении приложения.

window_effects.rs добавляет acrylic/mica/vibrancy/blur и window commands. В том же файле остались три placeholder-команды backend_send_message, backend_interrupt, backend_request_vitals, которые печатают в stdout и не являются реальным transport path. Сейчас App.tsx использует WebSocket напрямую, поэтому эти commands — мёртвый/устаревший API surface.

### 6.6. Дублирование frontend integration paths

Есть два конкурирующих слоя:

1. текущий путь: App.tsx → new WebSocketBackend(...);
2. legacy path: useBackendBridge.ts → createBackend() → stores/UI state.

SettingsPanel.tsx ещё импортирует cloudSettingsApi из legacy hook. Из-за этого часть UI работает через прямой production path, а часть — через старый adapter/store path. Это повышает вероятность расхождения состояния, readiness и confirmation semantics.

### 6.7. Fixture/preview режим

backend.ts содержит mock timeline, но он включается только при VITE_VISUAL_PREVIEW=1. Это хорошая граница: обычная сборка не объявляет backend готовым заранее, а preview остаётся отдельным режимом визуальной проверки.

---

## 7. Frontend ↔ backend протокол

### Client → server

| Message | Назначение |
|---|---|
| command | текстовая команда |
| confirm | ответ на pending high-risk confirmation |
| interrupt | остановка текущего execution path |
| ping | transport health |
| auth | optional token authentication |
| settings:get / settings:update | masked provider settings |
| hotkey_pressed | barge-in/global shortcut |
| voice_listen / voice_input | STT flow |
| wake_word / ambient_initiated | proactive signal |
| screen_capture | permission-gated screenshot |
| first_launch | first profile name |
| launcher actions | autostart/hotkey/greeting settings |

### Server → client

| Message | Назначение |
|---|---|
| state | entity state |
| runtime_status | readiness и diagnostics |
| profile | first-launch profile state |
| event | activity/timeline/mission event |
| assistant_output | typed final/stream output |
| confirmation_required | confirmation UI |
| vitals | CPU/RAM/runtime vitals |
| route | selected route/mode |
| mission_ack | accepted background mission |
| error | transport/action error |

События идут как typed envelopes, но внутри Python payload по-прежнему достаточно динамический. Следующий шаг качества — сгенерировать protocol schema из одного источника и валидировать обе стороны по JSON Schema или аналогичному typed contract.

---

## 8. Конфигурация и режимы запуска

### 8.1. config/settings.py

Pydantic-модели покрывают:

- API keys/endpoints;
- model tiers/providers;
- local GGUF model;
- coder model;
- voice/TTS/STT/wake word;
- paths;
- persona;
- limits и latency budgets;
- logging/proxy;
- launcher;
- shadow/brain policy;
- credential store.

API key precedence:

1. JARVIS_<PROVIDER>_API_KEY;
2. <PROVIDER>_API_KEY;
3. JSON config.

Config save использует temp file + atomic replace. Load выполняет validation и migration старого latency key.

### 8.2. Фактические значения против документации

Есть четыре слоя version/config identity:

| Источник | Значение |
|---|---|
| Python package | living-jarvis, version 0.1.0 |
| pyproject.toml description | Qwen/local-oriented description |
| Frontend package | jarvis-frontend, version 4.0.0 |
| Frontend README/types | labels v3.0 |
| Actual settings.json | DeepSeek-mode, cloud provider, remote model |
| Runtime manifest | packaged cloud/DeepSeek payload |

Это уже не косметика. Version drift влияет на диагностику, bug reports, support bundle и выбор runtime. Нужен один generated version manifest, из которого питаются Python package, frontend, README badge, runtime manifest и diagnostics.

---

## 9. Packaging и staged runtime

### 9.1. Заявленный packaging path

scripts/package_local_runtime.py:

1. собирает PyInstaller one-file backend;
2. подтягивает submodules для core, config, vector runtime;
3. кладёт Piper/STT/embedding/DPAPI resources;
4. использует production DeepSeek/cloud flags;
5. требует build-time provider secret из environment;
6. не должен оставлять loose core рядом с backend executable, чтобы старый source tree не shadowed runtime.

scripts/build_portable_installer.py:

- synchronizes staged runtime в Tauri release bundle;
- проверяет backend/Piper/STT/credential assets;
- отклоняет stale GGUF/llama-server payload;
- создаёт hidden launchers;
- строит 7zip SFX package.

### 9.2. Обнаруженное состояние staged folder

jarvis/src-tauri/resources/jarvis-runtime/runtime-manifest.json датирован 2026-08-22 и описывает cloud/DeepSeek packaged backend. При этом staged folder фактически содержит также loose directories config, core, data, persona, runtime.

Сравнение staged snapshot с текущим source tree показало:

- 171 совпадающий файл;
- 47 отличающихся;
- 244 отсутствующих в одной из сторон;
- в source subset отличаются backend-модули, включая config/settings.py, actions, agent.py, capabilities.py, cognitive/executive paths, model router, orchestrator, platform, repair, safety, structured, task runtime, verifier, voice и WebSocket server;
- в snapshot отсутствуют свежие source paths, включая части core/authority и core/understanding/semantic.py.

Это означает, что staged payload нельзя считать текущим исходным кодом. Его нужно пересобрать packaging script-ом непосредственно перед installer build и добавить CI-проверку, что staged tree соответствует manifest и не содержит запрещённый loose backend source.

**Релизный вывод:** текущая resources/jarvis-runtime — потенциальный blocker для reproducible installer. Исходники и production package сейчас не являются гарантированно одной и той же версией.

---

## 10. Data, persistence и секреты

Основные runtime-каталоги:

- data/brain — provider credentials/reference и brain state;
- data/authority — authority key/reference;
- data/executive — goals, commitments, world state;
- data/graph — graph database;
- data/cognitive — evidence/state;
- data/missions — task runtime persistence;
- data/models — local/voice model assets;
- data/logs — application logs;
- profile/relationship/taste/context stores;
- shadow backlog и pending research.

В runtime обнаружены DPAPI credential reference, authority key и database artifacts. Их содержимое не выводилось. Для репозитория следует отдельно проверить:

- чтобы .gitignore покрывал DPAPI, authority key, logs, databases и local model assets;
- чтобы installer не паковал пользовательские database/log files;
- чтобы diagnostics не раскрывал raw provider settings;
- чтобы export/support bundle применял secret filter до архивации.

Сильные места: atomic stores, masked settings, environment precedence, secret filtering и отсутствие raw secret payload в frontend event rail.

---

## 11. Проверка качества

### 11.1. Frontend production build

Команда: npm run build в E:\jarvis-2.4-main\jarvis

Результат: **PASS**

- 1 583 Vite modules transformed;
- CSS bundle около 37.66 kB;
- JS bundle около 207.63 kB;
- build завершён за 9.29 s.

### 11.2. Frontend contract tests

Команда: npm run test:frontend

Результат: **PASS**

- wsProtocol: 5 assertions;
- wsBackend: command, response, confirmation и masked settings;
- operatorModel: 17 assertions.

Покрыты protocol mapping, WebSocket command flow, reconnection-facing adapter behavior, settings masking и operator mission reducer logic.

### 11.3. Python syntax

Команда: python -m compileall -q main.py config core hud persona

Результат: **PASS**, exit code 0.

### 11.4. Полный Python test suite

Команда: python -m pytest -o addopts='' -q

Результат: **FAIL**, exit code 1.

~~~text
814 passed
5 failed
2 skipped
2 warnings
144.94 seconds
~~~

Падения:

1. tests/product_acceptance/test_r2_product_contracts.py::test_currency_request_uses_fresh_public_data_not_model_memory — verified public_data output не содержит ожидаемый символ валюты в naturalized text.
2. tests/product_acceptance/test_r2_product_contracts.py::test_verified_fast_action_survives_cloud_finalizer_outage — fast action в DeepSeek mode возвращает unverified runtime failure вместо verified result после отказа finalizer.
3. tests/product_acceptance/test_r2_product_contracts.py::test_youtube_fallback_extracts_first_video_instead_of_stopping_at_search — test seam ожидает core.actions.media.requests, но модуль не экспортирует этот объект.
4. tests/test_sprint10_mission.py::test_operator_mission_observes_repairs_only_mismatch_then_learns — фактический русский user_message отличается от контрактной строки.
5. tests/test_sprint12_personality.py::test_natural_completion_requires_verified_success — PersonalityEngine теряет часть ожидаемого обращения в verified completion text.

Warnings:

- deprecation warning для websockets.server.serve;
- deprecation warning для legacy websockets package path.

### 11.5. Состояние рабочей копии

До анализа уже был изменён tracked-файл:

E:\jarvis-2.4-main\tests\product_acceptance\test_r2_product_contracts.py

Отчёт не изменяет исходный код и не исправляет этот файл. Новым изменением является только данный Markdown-отчёт.

---

## 12. Найденные проблемы по приоритету

### P0 — блокирует доверительный installer release

#### P0.1. Staged runtime расходится с source of truth

**Место:** jarvis/src-tauri/resources/jarvis-runtime

**Симптом:** timestamp manifest старше текущего source snapshot; loose core присутствует рядом с packaged runtime; source comparison показывает десятки отличий и missing modules.

**Риск:** desktop installer может запустить не тот код, который прошёл тесты. Ошибки будут выглядеть как случайные расхождения между dev и installed app.

**Исправление:** всегда запускать package_local_runtime.py перед Tauri bundle, очищать staged directory, генерировать manifest из фактических bytes, добавлять CI diff check и запрещать loose backend source в release payload.

### P1 — исправить до следующей acceptance-сборки

#### P1.1. Readiness в cloud mode слишком оптимистичен

**Место:** core/ws_server.py, _runtime_status_payload.

В DeepSeek mode server возвращает state: ready и ready: true сразу по признаку включённого режима. Это означает «режим сконфигурирован», а не «provider reachable и отвечает». UI может показать ready перед реальной проверкой provider.

**Исправление:** readiness должен опираться на результат health/probe из orchestrator и различать configured, checking, ready, unavailable.

#### P1.2. Ambient TTS получает неправильный тип

**Место:** core/ws_server.py, ветка ambient_initiated.

_speak принимает typed AssistantOutput, но ветка передаёт в неё raw str. В результате proactive phrase попадает в event broadcast, но speech path пишет ошибку и не озвучивает текст.

**Исправление:** создать AssistantOutput.natural(text) перед _speak и добавить regression test на ambient event → TTS queue.

#### P1.3. Два frontend bridge path

**Места:** jarvis/src/App.tsx, jarvis/src/hooks/useBackendBridge.ts, jarvis/src/integrations/backend.ts.

Текущий App живёт на прямом WebSocket transport, но settings и legacy UI используют createBackend/legacy store layer.

**Исправление:** выбрать один canonical adapter; перенести settings API в него; удалить или явно изолировать legacy hook/store path.

#### P1.4. Tauri CSP отключена

**Место:** jarvis/src-tauri/tauri.conf.json, security.csp = null.

Для desktop shell с local backend это не даёт немедленной проблемы, но убирает важный слой защиты webview.

**Исправление:** задать минимальную CSP для self, локального dev origin и нужного WebSocket endpoint; проверить plugins/capabilities по принципу least privilege.

#### P1.5. Placeholder backend commands остались зарегистрированными

**Место:** jarvis/src-tauri/src/window_effects.rs.

backend_send_message, backend_interrupt, backend_request_vitals только печатают сообщение. Они создают иллюзию второго transport path и могут быть случайно вызваны будущим UI.

**Исправление:** удалить их, либо подключить к canonical WebSocket adapter и покрыть интеграционным тестом. Предпочтительно удалить Rust wrappers, если WebSocket остаётся единственным transport.

### P2 — исправить в ближайшем hardening cycle

#### P2.1. Вложения не передаются backend

App.tsx вызывает sendCommand(text, []), а wsBackend.ts при наличии files явно сообщает пользователю, что текущий protocol передаёт только текст. UI может показывать выбранный файл, но backend его не получает.

**Варианты:** либо убрать attachment affordance из UI до готовности протокола, либо добавить upload/reference envelope с size/type/hash и permission policy.

#### P2.2. Confirmation event может терять tool/risk

Синхронный state path формирует confirmation envelope с неполными полями, хотя pending state на backend содержит больше контекста. UI получает confirmation card без полноценного объяснения действия.

**Исправление:** сделать PendingConfirmation единым typed contract и передавать id, tool, risk, args summary, reason, expiry и mission id во всех ветках.

#### P2.3. Interrupt глобальный для всех клиентов

interrupt отменяет все активные missions. При нескольких UI clients один клиент может остановить работу другого.

**Исправление:** связать interrupt с connection/session/mission id; отдельный admin-wide cancel оставить отдельным явно именованным сообщением.

#### P2.4. Mission/event retention не ограничен

Нужны лимиты на количество миссий, событий, evidence и размер durable JSON. Queue cap ограничивает активные executions, но не историческое накопление.

#### P2.5. Версия и документация drift

pyproject сообщает 0.1.0, frontend package — 4.0.0, README/types — v3.0, текущая конфигурация и runtime manifest — cloud/DeepSeek mode. Historical architecture docs продолжают писать, что Agent path, routing, UI integration, STT и computer actions отсутствуют, хотя текущий codebase уже содержит большую часть этих слоёв.

**Исправление:** обновить docs после фиксации runtime contract и пометить старые документы как historical.

#### P2.6. Слишком крупные orchestration modules

agent.py, orchestrator.py, task_runtime.py, ws_server.py выполняют много ролей и медленно проверяются целиком.

Рекомендуемое разбиение:

- agent/conversation_gate.py;
- agent/capability_execution.py;
- agent/verification.py;
- orchestration/input_dispatch.py;
- orchestration/service_registry.py;
- runtime/persistence.py;
- runtime/scheduler.py;
- transport/command_handlers.py;
- transport/event_mapper.py.

Это не срочный rewrite. Сначала стабилизировать contracts, затем резать по seams, которые уже видны в текущем коде.

#### P2.7. Дублируется session ownership

Orchestrator и Agent имеют собственные session-related objects. Если они не используют один shared manager для каждого пути, разговорный context и persistence могут расходиться.

**Исправление:** один SessionManager на process, передаваемый dependency injection; отдельные views только для transport/UI.

#### P2.8. Legacy model/router paths требуют явного статуса

BrainFabric, ModelRouter, CouncilRouter и tier compatibility работают одновременно. Это повышает стоимость поддержки и затрудняет объяснение, почему выбран конкретный provider.

**Исправление:** пометить active path, compatibility path и sunset date; выводить в diagnostics фактическую цепочку route selection.

#### P2.9. WebSocket default security стоит сделать безопаснее

Основной launcher использует loopback и origin allowlist, что снижает поверхность. Но конструктор server с пустым allowed_origins допускает более широкий сценарий, а token auth optional.

**Исправление:** secure-by-default allowlist, обязательный per-install random token для packaged mode и explicit dev override.

---

## 13. Что уже сделано хорошо

1. **Единый typed action contract.** ToolContext и ActionResult дают общий execution surface.
2. **Верификация после действия.** Система не считает вызов инструмента успехом без postcondition.
3. **Ограниченный repair loop.** Нет бесконечного самопочиняющегося цикла.
4. **Durable missions.** TaskRuntime умеет pause/resume/restore и восстанавливает active mission после restart.
5. **Разделение conversation и action.** Conversation gate и deterministic fast paths снижают ненужные model calls.
6. **Fresh-data path.** Currency/weather/news/version идут через актуальные источники, а не через память модели.
7. **Secret hygiene.** Masked settings, DPAPI reference, redaction и отсутствие secret payload в UI events.
8. **Реальный frontend transport.** Production App использует WebSocket, а mock ограничен preview flag.
9. **Устойчивый launcher.** Tauri следит за backend process, умеет restart и скрытый Windows launch.
10. **Контрактные frontend tests.** Protocol, settings masking и mission reducer уже выделены в отдельные проверки.
11. **Atomic persistence.** Config и runtime stores используют temp/replace вместо прямой записи поверх файла.
12. **Модульная capability metadata.** Risk, permissions, requirements и evidence scope доступны не только в коде tool implementation.

---

## 14. Рекомендуемый план работ

### Этап 1 — сделать release воспроизводимым

1. Зафиксировать один version source.
2. Пересобрать staged runtime скриптом packaging.
3. Удалить loose backend source из release payload.
4. Сгенерировать manifest и hashes после сборки.
5. Добавить CI-проверку staged/source/runtime consistency.
6. Проверить installer на чистой Windows machine.

### Этап 2 — закрыть функциональные регрессии

1. Исправить ambient TTS type mismatch.
2. Исправить readiness semantics и health state.
3. Исправить пять Python test failures.
4. Добавить tests на confirmation payload completeness.
5. Добавить tests на interrupt scope.
6. Зафиксировать attachment behavior: реализовать или убрать UI affordance.

### Этап 3 — свести frontend architecture

1. Сделать WebSocketBackend единственным adapter.
2. Перенести cloudSettingsApi в canonical integration module.
3. Удалить unused useBackendBridge/placeholder commands или пометить compatibility boundary.
4. Сгенерировать protocol types/schema.
5. Проверить overlay/main/tray на одном shared connection lifecycle.

### Этап 4 — hardening и эксплуатация

1. Включить restrictive CSP.
2. Минимизировать Tauri capabilities/plugins.
3. Включить secure-by-default WS auth для packaged mode.
4. Добавить mission/event retention policy.
5. Добавить health/readiness telemetry с provider latency и failure reason.
6. Сократить крупные modules по execution seams.
7. Обновить architecture docs и release runbook.

---

## 15. Практический runbook

### Backend console

~~~powershell
Set-Location E:\jarvis-2.4-main
python main.py
~~~

### Backend WebSocket development server

~~~powershell
Set-Location E:\jarvis-2.4-main
python -m core.ws_server
~~~

### Frontend build

~~~powershell
Set-Location E:\jarvis-2.4-main\jarvis
npm install
npm run build
npm run test:frontend
~~~

### Python checks

~~~powershell
Set-Location E:\jarvis-2.4-main
python -m compileall -q main.py config core hud persona
python -m pytest -o addopts='' -rA
~~~

### Tauri development

~~~powershell
Set-Location E:\jarvis-2.4-main\jarvis
npm run tauri dev
~~~

### Release order

~~~text
source checks
  → Python tests
  → frontend build/tests
  → package_local_runtime.py
  → staged manifest/hash verification
  → Tauri release build
  → clean-machine smoke test
  → installer artifact
~~~

Не следует строить installer напрямую из существующей resources/jarvis-runtime без regeneration и verification.

---

## 16. Карта ключевых файлов

### Backend entry/config

- main.py — console entrypoint;
- pyproject.toml — Python package metadata;
- config/settings.py — typed settings, env precedence, atomic save/load;
- config/settings.json — фактическая local runtime configuration;
- config/settings.example.json — packaging/config template;
- persona/* — identity/style data;
- hud/* — legacy/console-facing UI helpers.

### Backend orchestration/runtime

- core/orchestrator.py — composition root and input dispatch;
- core/agent.py — agent loop, routing, tool execution, verification, memory;
- core/task_runtime.py — mission lifecycle, queue, persistence, events;
- core/ws_server.py — WebSocket server and protocol handlers;
- core/verifier.py — strict verifiers;
- core/repair/* — repair strategy and attempts;
- core/structured/* — structured plans/results;
- core/router/* — route guards, intent and legacy routing;
- core/authority/* — effect classification and grants.

### Backend brain/understanding

- core/brain/* — typed roles, providers, health, policy, fabric;
- core/llm/* — legacy tiers, local/remote adapters and model routing;
- core/understanding/* — route classification, quick answer, semantic mission;
- core/intelligence/* — task contracts, universal intake, tutor, skills;
- core/cognitive/* — continuity, self-model and cognitive state;
- core/cognitive_kernel/* — mission ledger and evidence contracts.

### Backend memory/executive

- core/memory/* — short-term, graph, Chroma, RAG, profile, relationship, taste;
- core/executive/* — goals, commitments, world, command OS, learning, storage;
- core/metacognition/* — calibration, beliefs, failure analysis, freshness;
- core/security/* — atomic stores, redaction, credential boundaries;
- core/shadow/* — rehearsal, backlog, pattern generation;
- core/living/* — context sampler, proactive intelligence, workflows;
- core/proactive/* — scheduler/proactor/background work.

### Backend actions/platform/voice

- core/actions/* — registered capabilities and tool implementations;
- core/platform/* — browser bridge, Windows operations, computer use;
- core/operator/* — mission/operator/software semantics;
- core/voice/* — Piper, STT, TTS queue, wake word;
- core/vision/* — screenshots and visual state;
- core/triggers/* — system monitors and trigger engine;
- core/utils/* — logger, paths, model manager.

### Frontend

- jarvis/src/App.tsx — current UI composition point;
- jarvis/src/integrations/wsBackend.ts — live WebSocket adapter;
- jarvis/src/integrations/wsProtocol.ts — envelope mapping;
- jarvis/src/integrations/backend.ts — adapter factory and preview backend;
- jarvis/src/hooks/useBackendBridge.ts — legacy/duplicate adapter hook;
- jarvis/src/types/index.ts — UI/backend type definitions;
- jarvis/src/stores/sessionStore.tsx — session timeline state;
- jarvis/src/operator/* — operator mission model/UI;
- jarvis/src/components/* — shell, sidebar, composer, drawer, settings, confirmation;
- jarvis/src/styles/* — themes, presence, atmosphere and visual system;
- jarvis/src-tauri/src/main.rs — Tauri process/window/tray launcher;
- jarvis/src-tauri/src/window_effects.rs — window effects and old placeholder commands;
- jarvis/src-tauri/tauri.conf.json — windows, bundle, resources and security;
- jarvis/src-tauri/capabilities/default.json — allowed Tauri permissions;
- jarvis/src-tauri/resources/jarvis-runtime — generated package staging area.

### Documentation

- README.md — current product/startup overview;
- docs/jarvis_core_architecture.md — historical architecture document;
- docs/architecture/00_current_state_and_gaps.md — historical gap inventory, уже не полностью соответствует HEAD;
- docs/* — sprint/verification/release material.

---

## 17. Финальная оценка

### Архитектура

**8/10 для функционального alpha.** Слои уже выделены, execution path полноценный, есть verification, repair, authority, durable missions и typed transport. Снижают оценку orchestration monoliths, duplicate routes и старые compatibility seams.

### Кодовая база

**7/10.** Хорошая глубина и заметная инженерная дисциплина: dataclasses/types, atomic stores, retries, health/circuit breaker, test contracts. Снижают оценку размеры ключевых файлов, dynamic payload casts и drift между документацией и кодом.

### Frontend

**7.5/10.** Production UI реально подключён к backend и хорошо разделяет visual shell, events, mission rail и transport. Главные долги — двойной bridge, placeholder commands, attachment gap и CSP.

### Release readiness

**5.5/10 до исправлений.** Frontend build зелёный, но полный Python suite красный, staged runtime не гарантированно синхронен с source, а version identity распадается на несколько значений.

### Приоритет одной строкой

Сначала привести staged runtime и version manifest к одному source of truth, затем закрыть пять тестовых регрессий и четыре transport/UI contract gap. После этого desktop installer станет проверяемым артефактом, а не просто успешной компиляцией.

---

**Отчёт завершён. Исходный код и runtime state не изменялись; создан только этот файл анализа.**
