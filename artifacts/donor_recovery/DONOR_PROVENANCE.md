# Donor Provenance

Baseline commit: `ce1af9584756df228ecc8a03e9eb5bca5900d224`.

## Adaptations

| Donor | Ref | Source file | License | Adapted mechanism | JARVIS destination |
|---|---|---|---|---|---|
| pipecat-ai/pipecat | `3275bf619af0bd731f9dcc3a2be51ad0c5f59029` | `src/pipecat/processors/audio/audio_buffer_processor.py` | BSD-2-Clause (SPDX header) | Concept only: explicit turn/cancel lifecycle and generation-bounded audio; implementation written for current queue | `core/voice/tts_queue.py` |
| KoljaB/RealtimeSTT | `7a0b47607f634df8f6c448bcfbd4e9f2b6bdb02f` | `RealtimeSTT/core/lifecycle.py` | MIT repository license gate | Concept only: monotonic recording generation and lifecycle reset; implementation written for current STT boundary | `core/voice/stt.py` |
| leon-ai/leon | develop `c764f9dd4d78dff21f003ba4e48b3bcffe498b74` | `core/context/ARCHITECTURE.md` | MIT stated by repository `LICENSE.md`/README; no source copied | Context-first and terminal missing-settings semantics | existing orchestrator paths |
| snakers4/silero-vad | master, file blob `8f6b68191b6939a9ef92f7532935d7e7b6de530e` | `src/silero_vad/utils_vad.py` | MIT | Optional dependency interface and state reset behavior; no donor source copied | `core/voice/stt.py` |
| microsoft/UFO | `cd9bfdd6caacee7b8c5894605f42207ec84b6e47` | `ufo/automator/ui_control/inspector.py` | MIT repository gate; no source copied | UIA-first inspection principle | existing computer-use modules |
| browser-use/browser-use | `9a2db2d2db42c6f68a871f011b3b25fdcaa71847` | `browser_use/browser/watchdogs/default_action_watchdog.py` | MIT repository gate; no source copied | post-action state reconciliation principle | existing BrowserBridge |
| MODSetter/SurfSense | current default branch | only Apache-2.0 paths; `surfsense_backend/app/proprietary/` excluded | Apache-2.0 outside proprietary subtree; BSL-1.1 subtree excluded | source/citation and study-loop concepts only | existing document RAG/study workflow |

No substantial donor code is copied. Adaptations are clean, minimal implementations against JARVIS canonical interfaces. License texts and notices must be added to `THIRD_PARTY_NOTICES.txt` only when a donor dependency or copied substantial portion ships.

## Rejected from shipping runtime

- Chatterbox: source MIT, but multilingual weights and full dependency footprint require a separate isolated benchmark/license record before shipping.
- LiveKit turn-detection models: separate model license not assumed.
- SurfSense `surfsense_backend/app/proprietary/`: Business Source License 1.1, excluded.
- Any repository/file whose exact license could not be retrieved: ideas only, no code.
