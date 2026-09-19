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

**One baseline per platform.** The UI font stack resolves to different physical fonts on macOS
and Linux, so text-dense pages differ by a fraction of a percent of pixels between the two. The
snapshot name therefore carries the platform (`snapshotPathTemplate` in `playwright.config.ts`,
`{arg}-{platform}{ext}`), and each environment compares against pixels rendered on itself:

```text
frontend/e2e/visual.spec.ts-snapshots/blank-editor-light-linux.png     ← asserted by CI
frontend/e2e/visual.spec.ts-snapshots/blank-editor-light-darwin.png    ← asserted on macOS
```

Both sets are committed on purpose. A single shared set cannot work: whichever renderer owns it,
the other one drifts against it by more than the 1.5% threshold — which is how 8 of the 10
snapshots failed in CI at commit `c60f5be` (before any M2 change) while the same files passed on
the machines that produced them. That permanently red job also skipped the shared-mode security
acceptance behind it, so a rendering difference was silently disabling a security gate.

**When the UI changes on purpose, regenerate — do not "fix" the assertion.** Adding controls to
a panel legitimately changes the pixels of every screenshot that contains that panel, so a new
failure on a changed region is expected. Regenerate the set for your platform, review the
rendered images, and commit them as their own deliberate change:

```bash
# macOS: rebuild the E2E bundle first — `npm run build` overwrites dist/ with a bundle that
# has no test bridge, and every screenshot scenario then times out waiting for it.
cd frontend && npm run build:e2e && npx playwright test e2e/visual.spec.ts --update-snapshots
```

For the Linux set use `.github/workflows/visual-baselines.yml` (manual `workflow_dispatch`),
which runs the same setup as `Browser acceptance · Chromium`, updates the snapshots instead of
asserting them, and uploads the PNGs as an artifact; committing them is still a reviewed change.

That workflow is a real gate, not a rubber stamp. `--update-snapshots` rewrites the expected
images, so the pixel assertions are meant to pass; but a browser that cannot launch, a server
that will not start, a test that throws, or a run that produces **no** new PNGs must still fail
the job. An unconditional `|| true` around the Playwright invocation is prohibited for exactly
that reason: it hides those failures and can publish a green artifact containing baselines that
were never generated. The job therefore runs Playwright bare and then asserts that at least one
baseline file was actually rewritten.

The renderer is pinned (`ubuntu-24.04`) rather than `ubuntu-latest`. A baseline is only meaningful
next to the environment that produced it, so this job must not silently move to a new Ubuntu (and
a new browser/font stack) and regenerate a whole set of images under a changed environment; the
job also prints the runner OS and Python version so the environment is recorded per run.

Do not update snapshots merely to make a run green, and never regenerate a set in a renderer
other than the one that asserts it: that bakes the wrong font metrics into the baseline.

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
- Regenerate only the set belonging to the platform you are on, and only after looking
  at the images: `--update-snapshots` on macOS writes the `*-darwin.png` files, the
  `Visual baselines` workflow writes the `*-linux.png` files. Running the update on the
  wrong platform for a set is what produced the cross-renderer debt in the first place.
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

## Baseline generation must be able to fail (M4-0)

Snapshot generation runs on a **pinned** `ubuntu-24.04` runner, and the Playwright update command
is itself a required-success step: it is never wrapped in unconditional error suppression
(`|| true`), so a browser that fails to launch, a web server that fails to start or a test that
throws fails the job instead of publishing a green artifact that contains no usable baselines.

Proving that the step produced baselines is tied to **that step** and is done by a **sentinel**:
immediately before `--update-snapshots`, the workflow removes one committed baseline *for this
renderer* (and keeps a copy), and afterwards requires that file to exist again and to be newer than
the marker. A missing snapshot is written unconditionally, so its recreation proves the update
command really ran here and could write baselines. The suite is then run once more in **assert
mode**, so the baselines left in the tree must assert clean in the renderer that wrote them — a
regenerated set nobody can reproduce fails the job instead of being published.

Byte equality with the committed file is deliberately *not* required: drift is defined by
Playwright's pixel tolerance, not by PNG bytes, and a screen with a timing-dependent element can
re-encode a few bytes differently while still asserting clean.

Two weaker checks were tried and rejected, and both failures are worth remembering:

* a wall-clock window (`find ... -newermt '-30 minutes'`) passes on a fresh checkout, where git
  itself has just written every committed PNG and they all look recently modified;
* a marker with no sentinel ("at least one PNG is newer than the marker") fails on a **correct**
  no-drift run, because Playwright only rewrites snapshots that actually differ — and replacing the
  file with a deliberately different image does not help either, because Playwright refuses to
  update a snapshot whose dimensions differ, so the job fails on the case it exists to handle.

Renderer, browser and font versions used for a baseline are recorded with the baseline procedure, and
the regenerated set is uploaded as an artifact for review — the workflow commits nothing.
