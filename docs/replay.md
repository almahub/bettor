# Offline replay

Every new run stores its exact input manifest: normalized snapshots, provider failures, historical validation observations, portfolio, request, seed, resolved settings, model versions, prompt file hashes and optional fixture provenance. The SHA-256 identifies the analysis and deterministic run. SQL `recorded_at` records actual persistence time; `as_of` is the evidence cutoff.

`better replay-pipeline --analysis-id ID` verifies the manifest, software/schema/prompt/config/model versions, identifiers and temporal boundaries, then reruns the actual agents against isolated snapshot providers and an in-memory append-only journal. It compares both the decision and every agent output. No provider or inference endpoint is contacted. Historical child analyses require their parent. Tampering, missing required records and changed prompts fail explicitly.

System One is disabled during deterministic pipeline replay. If System 2 was used, only archived structured responses with matching request hash/model/prompt/temperature can be replayed. This reproduces the original qualitative output; it does not claim that a new LLM call would be deterministic.

`better replay --analysis-id ID` is a different operation: it evaluates the saved shadow states through the currently configured SystemOneProvider, compares semantic suggestions and measures would-skip valid opportunities. It requires System One enabled in shadow mode; choose the fake provider for offline testing. Task-specific benchmark datasets use `EvaluationCase` and `evaluate_dataset` in `better.system_one.replay`; calibration/test overlap and future data are rejected. No benchmark-based routing promotion is implemented.
