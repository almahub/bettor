# Testing

Verification, 2026-10-02: **402 Python tests passed** (`python -m pytest -q`) and
the dashboard controller test passed (`node tests/ui/test_dashboard.mjs`). Ruff
lint is clean. Coverage now includes External/Football Data Research, API Control,
central pipeline/checkpoints, settlement/validation and strict native competition
bindings for 888. A Starlette/TestClient deprecation warning remains upstream.

Run `python -m pytest -q` from the repository with the installed development environment. The configured temp directory stays inside `.cache/pytest`. Tests are offline and use explicitly synthetic football data; Rizzo/football-data HTTP tests use `httpx.MockTransport`.

Coverage includes schema and agent contracts, prompt/tool boundaries, deterministic formulas, probabilities and uncertainty, independent validation, all decision states, many positive reports blocked by hard gates, related evidence and future-data rejection, exact stake caps, provider failures and conflicts, System 2 structured output/repair, immutable journal and settlement, API/CLI/rate limiting, deterministic input replay and schema-valid HTTP adapter mappings.

System One regressions compare the entire AnalysisRun in disabled and shadow modes across successful, wait and rejected cases. Hostile suggestions cannot skip agents or change System 2 calls. Disabled mode makes no provider calls and does not construct observer state. Timeouts, refusal, malformed responses, model mismatch, abstention, retries and circuit breaker behavior are covered.

Static checks: `python -m ruff check better tests` and `python -m ruff format --check better tests`. Import compilation: `python -m compileall -q better`. Alembic upgrade to `0001` has been exercised against SQLite. Packaging is built and checked for bundled prompt files.

One upstream deprecation warning may appear from Starlette's TestClient/httpx integration in the installed dependency versions; it does not cause a failed assertion. Real PostgreSQL, real football-data account access, local Rizzo inference and a commercial System 2 service are not required by or represented as covered by the offline suite.

Dashboard tests cover six genuine pipeline outcomes through the demo API, persisted summaries and journal records, forbidden synthetic activation in ordinary mode, static asset MIME/CSP, shared rate limiting, safe templates and URL schemes. Controller tests generate real API contracts offline, then exercise every page/tab, history filtering, reopening and evidence dialogs against a minimal DOM adapter. This is not browser automation or visual verification. The current computer-use inventory exposes no browser, so desktop/mobile appearance and browser accessibility remain unverified.

Runtime 0.1.1 adds 19 tests for explicit degradations, mandatory model metrics, critical vetoes, market scoping, confidence caps, legacy safety and distinct source/agent logging. An explicit System 2 review now caps confidence; the existing qualitative-injection test uses a completed response without a review request, while a new test covers the conservative review behavior. Full suite: 259 Python + 5 JavaScript. See [audit](partial-and-observability.md).

888 integration final verification: 278 Python tests and 5 JavaScript tests pass. The 19 additional native-adapter cases cover selective requests, source timestamps/cache, supported markets, missing/ambiguous identities, inactive/suspended events and quotes, explicit HTTP failures, and zero-stake missing-data decisions. A direct live catalogue check after server restart returned 143 events and left decision counters empty. See [888 verification](provider-888.md).
