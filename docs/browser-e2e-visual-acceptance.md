# Browser E2E and visual acceptance

P&ID-Agent uses Playwright to validate real browser behavior against the real FastAPI document service and a production Vite build. The suite is deterministic: every test creates an isolated SQLite database, creates its own engineering document through REST, and never calls a real model provider.

## Install

Install the normal project and frontend dependencies first:

```bash
python -m pip install -e ".[mcp,dev]"
cd frontend
npm ci
npx playwright install --with-deps chromium
```

On a workstation where Chromium runtime dependencies are already installed, `npx playwright install chromium` is sufficient. CI always uses `--with-deps`.

## Run headless or headed

From `frontend/`:

```bash
# Build the deterministic E2E bundle and run every browser check.
npm run test:e2e

# Open Chromium and show the interactions.
npm run test:e2e:headed

# Run one file or one named scenario.
npm run build:e2e
npx playwright test e2e/engineering.spec.ts
npx playwright test --grep "Agent ghost preview"
```

The Playwright web servers start:

- FastAPI on `127.0.0.1:8000`, with a unique database and diagnostics file under `frontend/test-results/`;
- the production Vite preview on `127.0.0.1:4173`.

Both ports are overridable, and so is the preview's API proxy target, so a second
checkout can run the suite while another one already holds the defaults:

```bash
cd frontend
PID_AGENT_E2E_API_PORT=8011 PID_AGENT_E2E_PREVIEW_PORT=4174 npx playwright test
```

| variable | default | meaning |
| --- | --- | --- |
| `PID_AGENT_E2E_API_PORT` | `8000` | port the FastAPI web server listens on |
| `PID_AGENT_E2E_PREVIEW_PORT` | `4173` | port the Vite preview web server listens on |
| `PID_AGENT_API_TARGET` | `http://127.0.0.1:8000` | backend the preview proxies `/api` to (set automatically by the Playwright config) |
| `PID_AGENT_E2E_API_ROOT` | `http://127.0.0.1:8000/api/v2` | backend the fixtures call directly (set automatically by the Playwright config) |

CI passes none of these and keeps the defaults.

The suite is **destructive by contract**: `resetDocuments()` deletes every document
in the database it is pointed at. The web server always creates its own scratch
database under `frontend/test-results/`, which is why the suite can be run locally
at all — but do not point the fixtures at a live/shared database (`PID_AGENT_E2E_API_ROOT`)
just to "reuse" a running backend.

The E2E build exposes a test-only bridge for reading structured workspace state and injecting a deterministic Agent preview. Normal development and production builds do not expose that bridge.

## Failure evidence and traces

A failing scenario retains a screenshot, trace, video, and browser/API context under `frontend/test-results/playwright/`. The HTML report is written to `frontend/test-results/playwright-report/`.

Open a trace with:

```bash
cd frontend
npx playwright show-trace test-results/playwright/<scenario>/trace.zip
```

In GitHub Actions, the `Browser acceptance · Chromium` job uploads these directories only when the job fails. Artifacts are retained for seven days. The job uses deterministic fixture data and no Provider API key.

## Visual baselines

Visual snapshots use a fixed 1440 × 960 viewport, `zh-CN`, the `Asia/Shanghai` timezone, reduced motion, a fixed test clock, deterministic fixture names, and an E2E stylesheet that disables animation and pins the UI font stack. Baselines live in:

```text
frontend/e2e/visual.spec.ts-snapshots/
```

Update them only after reviewing the rendered result:

```bash
cd frontend
npm run test:e2e:update
```

**Regenerate them in the same renderer that verifies them.** The UI font stack resolves to
different physical fonts on macOS and Linux, so text-dense pages drift by a fraction of a
percent of pixels and a snapshot captured on one system fails on the other.

**Current owner: the CI renderer (Linux).** The whole set was regenerated on the Linux runner
in `.github/workflows/visual-baselines.yml` (manual `workflow_dispatch`), which runs the same
setup as `Browser acceptance · Chromium` and then updates the snapshots instead of asserting
them, uploading the PNGs as an artifact; the commit is reviewed and made by hand.

The history that produced them: the set used to be macOS-rendered, and 8 of the 10 snapshots
failed in CI at commit `c60f5be` (before any M2 change) purely from that cross-renderer drift,
which is why the whole Browser job stayed red and skipped the shared-mode security acceptance
behind it.

**When the UI changes on purpose, regenerate — do not "fix" the assertion.** Adding controls
to a panel legitimately changes the pixels of every screenshot that includes that panel, so a
new failure on a changed region is expected. Re-run the `Visual baselines` workflow after the UI
change itself is committed, review the images, and commit the new PNGs as their own deliberate
change. Do not update snapshots merely to make CI green, and never update them on a workstation
whose renderer differs from CI's: that bakes the other system's font metrics into the baseline
and reproduces the same failure from the opposite side.

**Working on macOS.** With Linux-owned baselines, a local `npx playwright test` reports those
snapshots as differing by a small fraction of pixels. That is the same drift in the mirror
direction, not evidence about the change you are making: judge a suspected visual regression by
the worktree A/B below and by the differing-pixel count, not by the red/green of a local run.
The authoritative comparison is the CI job.

### Proving "pre-existing, not introduced" with a worktree A/B

"Probably pre-existing" is not evidence. When a snapshot fails locally after a change,
measure both sides on the same machine in the same session:

```bash
# 1. check out the last accepted baseline next to the working tree
git worktree add .freebuff/baseline <accepted-baseline-sha>
# 2. reuse the installed dependencies instead of reinstalling them
ln -s "$(pwd)/frontend/node_modules" .freebuff/baseline/frontend/node_modules
# 3. build the E2E bundle there and run only the suspect scenarios
cd .freebuff/baseline/frontend && npm run build:e2e
npx playwright test e2e/visual.spec.ts -g "locked element badges|connector route anchors"
# 4. clean up (remove the symlink first: worktree remove --force also needs it gone)
rm -f .freebuff/baseline/frontend/node_modules
git worktree remove --force .freebuff/baseline
```

Playwright reports the differing pixel count per failure, which is the number to compare.
Measured example at the M2 accepted baseline `49e3e14` versus the M3 working tree:
`locked element badges` 22941 → 22868 pixels, `connector route anchors` 22574 → 22501
(both ratio 0.02, threshold 0.015) — the same failures at the same magnitude, i.e. a
baseline drift of the same size, not a regression from the change. The committed
baselines for those two files were last touched at `61cc572` (2026-08-21), before M2.

### Checking a regenerated baseline before committing it

The `Visual baselines` workflow renders in the same environment CI asserts in, so the
regenerated PNGs can be checked against CI's own captures of the *previous* commit: download
the `playwright-failure-*` artifact of the failing run, and compare each regenerated baseline
with the newest `*-actual.png` CI produced for the same snapshot. Measured that way at the CAD
import slice (Linux baselines versus the Linux actuals of the run before them): 0.0003–0.0011
differing ratio across all nine regenerated files, i.e. below the 0.015 threshold with two
orders of magnitude to spare — which is the evidence that the new baselines *will* pass, rather
than a hope that they might.

Consequences to keep in mind:

- A screenshot failure is **weak evidence**: it can be a threshold-crossing of a
  pre-existing renderer drift rather than a code change. Always compare against the
  failure set of the *baseline* commit before blaming the current diff, and read
  `*-expected.png` / `*-actual.png` / `*-diff.png` rather than the summary line.
- Do not "fix" a red snapshot by running `npm run test:e2e:update` on a macOS
  workstation: that bakes the Darwin font metrics into the baseline and reproduces
  the same failure from the other side. Regenerating the whole set belongs in the CI
  renderer (the `Visual baselines` workflow, artefacts reviewed and committed) and
  should be its own deliberate change.
- Prefer a **layout assertion** over a pixel snapshot when the property is
  structural. The dock tab strip is guarded by a real assertion ("every tab stays on
  one row inside the dock") in `e2e/engineering-graph.spec.ts`, precisely because a
  `repeat(N, …)` grid silently reflowed the page when a tab was added.

A product regression is a change that was not intended by the implementation: clipped controls, lost engineering elements, changed connector colors, missing lock/anchor/ghost affordances, incorrect panel state, or unstable text/layout. A legitimate visual change is an explicitly reviewed product modification whose affected screenshots match the approved design. Do not update snapshots merely to make CI green; inspect the actual, expected, and diff images first.

Baselines must never contain API keys, authorization headers, local user paths, random IDs, private engineering data, videos, traces, or generated reports. Only the reviewed PNG baselines are committed.

## Automatic acceptance coverage

These checks must be run through the npm script or after an explicit E2E build:
`npx playwright test` alone reuses whatever `dist/` currently exists, so running
`npm run build` (production, no test bridge) and then the raw Playwright CLI points the
browser at a bundle the fixtures cannot drive — the symptom is a green test turning red
with the canvas never appearing. `npm run test:e2e` runs `build:e2e` first for exactly
that reason.

The pull-request job automatically verifies:

- document creation and persistence after reload;
- browser placement of two devices and a connector drawn from real source/target ports;
- endpoint binding and orthogonality after moving connected equipment;
- junction/branch topology and `main_route_id` preservation;
- route segment editing, obstacle avoidance, locked route anchors, and bend insertion;
- alignment, distribution, grouping, group movement, element locks, and mixed-value bulk editing;
- one canvas command and one selection command through the command palette;
- inline insertion of a two-port device into a main line;
- light/dark appearance without engineering SVG color or revision changes;
- named views, minimap navigation, automatic large-diagram zones, and fit-selection;
- deterministic Agent ghost preview without a revision change, followed by apply and undo;
- deterministic drafting analysis, preview, apply, lock/unlock and API read-only/reproducibility checks (`e2e/drafting.spec.ts`);
- ten visual regression states;
- opening, zooming, panning, selecting, minimap navigation, and fit-selection on a 500-element drawing with deliberately broad CI limits.

Assertions inspect the persisted document, element counts, revisions, endpoint IDs and port IDs, route metadata, lock/group metadata, styles, labels, and orthogonal points. They do not pass solely because a button is present.

## Manual acceptance still required

The normal pull-request workflow does not replace:

- real-provider model-matrix checks with user-supplied credentials;
- subjective review of complex, production-scale engineering drawings;
- the separate 1000/2500/5000-element benchmark suite;
- platform-specific headed checks outside Chromium/Linux;
- review of a visual baseline change before the new PNG is accepted.
