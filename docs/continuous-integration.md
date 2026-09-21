# Continuous integration

GitHub Actions runs deterministic validation on every pull request and every push to `main`.

## Automated jobs

The workflow in `.github/workflows/ci.yml` runs four bounded Ubuntu jobs:

- **Backend · Python 3.11** installs `.[mcp,dev]`, runs `ruff check backend`, the offline
  `pid-agent quality-harness`, then `pytest -q`;
- **M5 self-repair · 72-case deterministic gate** runs `pid-agent repair-benchmark --suite dev`
  and then `--suite acceptance --candidate-sha ${{ github.sha }}`, which derives all 72 cases
  from that commit, verifies the published evidence with the independent verifier, and exits 2
  unless every frozen threshold holds. It then runs the §G scale track with
  `pid-agent repair-scale --candidate-sha ${{ github.sha }}` on the large synthetic drawing, and
  asserts that `pid-agent repair-qualification` exits **3** — the code that means "no model
  credential", which must never be mistaken for a pass or for a failed candidate. The summary and
  the full per-case evidence are uploaded as an artifact for 30 days, so the release verdict is
  re-computable after the run;
- **Frontend · Node 24** installs the locked npm dependency graph with `npm ci`, runs the frontend pure-function suite, then the production TypeScript/Vite build;
- **Browser acceptance · Chromium** installs the project and locked frontend dependencies, restores the Playwright browser cache keyed by `package-lock.json`, installs Chromium/runtime dependencies, builds the deterministic E2E bundle, and runs the engineering E2E, visual regression, and 500-element performance smoke suites.

The backend runner installs the Cairo runtime required by SVG/PNG/PDF export tests and validates ISO page boxes, title blocks, pagination and PDF metadata. DXF tests additionally load every representative AC1027 file with the independent `ezdxf` parser, run its audit, and verify layers, entities, units, XDATA and limits. Engineering-report tests validate stable equipment/line/instrument schedules, rule codes, hidden-layer scope, CSV encoding and revision immutability. The browser job starts a real FastAPI service and production Vite preview with an isolated SQLite database. It never uses a real model-provider API key or user project data. Agent scenarios use deterministic test data and a test-only preview injection bridge.

The self-repair gate is separate from the quality harness on purpose: the harness pins the *rules*
that make a success rate mean something (frozen spec and generator fingerprints, SHA-derived cases,
recomputable counts, a 100% safe negative suite, exactly one governed write), while the 72-case run
is the measurement itself. Both are deterministic and need no Provider credentials. The contract,
the six defect families and the mechanical definitions of locality and protected regions are in
[`m5-agent-self-repair.md`](m5-agent-self-repair.md).

The scale track is in that same job because it is the same claim at a different size: CI builds the
large synthetic drawing (the private 939 KB DWG is not in the repository), runs the five frozen
cases, and publishes scope size, context bytes, attempts and timings per case. The real-file run,
including its SHA-256, is part of the local acceptance evidence instead. The real **model**
qualification is deliberately *not* a CI check: GitHub Actions has no provider credential, so CI's
job is to prove the honest status is reported, which is why the step asserts exit code 3 rather
than tolerating whatever the command returns.

The offline quality harness audits every dynamically loaded symbol, renders the active catalog,
commits a representative port/junction topology to temporary SQLite, and compiles both valid and
known-invalid model-shaped semantic plans. It never calls a Provider and never opens the user's
document database. See [`offline-quality-harness.md`](offline-quality-harness.md).

Retries and timeouts are explicit: Playwright uses one worker, at most one retry in CI, a 45-second per-test limit, bounded web-server startup, and a 30-minute job limit. Core scenarios are not conditionally skipped. On failure, the job uploads screenshots, traces, videos, the HTML report, and diagnostics for seven days.

Detailed local commands, visual baseline review, and trace inspection are documented in [`browser-e2e-visual-acceptance.md`](browser-e2e-visual-acceptance.md).

## Manual acceptance checks

The following remain manual because they require external services, credentials, subjective engineering review, other platforms, or longer execution time:

- real-provider `pid-agent model-matrix` runs;
- complex production drawing review;
- 1000/2500/5000-element browser benchmarks;
- non-Chromium/Linux headed acceptance;
- approval of intentional visual changes before snapshot updates;
- rasterized review of intentional PDF/title-block layout changes on representative engineering drawings;
- opening representative DXF files in the target CAD applications when interoperability behavior intentionally changes.

Manual reports should be committed under `reports/` only after confirming that API keys, authorization headers, model prompts, local paths, and confidential engineering data are absent.
