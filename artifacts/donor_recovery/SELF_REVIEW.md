# Adversarial Product Review

- **Canned dialogue:** confirmed repeated honorific in live baseline. Fixed at production response boundary and strengthened the single-persona prompt. Live retest produced four distinct direct responses with no honorific.
- **Voice interruption:** confirmed stale FIFO tail risk. Fixed with monotonic generations; interrupted text is cleared and never recorded as spoken context.
- **Duplicated speech:** completed playback is recorded once; queue generation prevents an old turn from entering spoken history after barge-in.
- **Heavy default voice:** Chatterbox dependency/weights gate did not justify installation on the 8 GB CPU target. Piper remains default. Two real Russian Piper voices were benchmarked and samples retained; Windows SAPI candidate produced empty/header-only WAVs and was rejected.
- **Study usefulness:** confirmed DocumentRAG returned anonymous text chunks only. Added source/sha/chunk evidence and a bounded quiz/weak-topic retry state on the canonical DocumentRAG layer.
- **Computer use:** no confirmed architecture gap requiring code. Existing BrowserBridge is already DOM-first, rejects stale DOM, preserves session identity, and verifies post-action state. No VLM or competing Hands layer added.
- **Frontend state:** confirmation was rendered as generic executing and waiting/repair were not distinguished. Presence metadata now exposes NEEDS_USER, WAITING, and REPAIRING from real events.
- **Privacy:** study results are bounded to `top_k <= 20`; no bulk filenames/processes/screens are added to model context.
- **License:** no substantial donor code copied. BSL/proprietary SurfSense subtree, unverified model weights, and unresolved-license repositories stayed out of runtime.
- **Remaining release risks:** Chatterbox human-quality A/B is blocked by separate weights/license and hardware feasibility gates; Silero package is optional and was not installed; full microphone acoustic false-positive testing needs recorded/noisy fixtures or a live mic session.
