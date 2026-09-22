# M5 release readiness

**Release anchor: `a92044f`** — `docs(m5): record the A6 qualification readiness instead of guessing at it`
CI run **35708328119** on that commit: **all four jobs success** (M5 72-case deterministic gate · Backend · Python 3.11 ·
Frontend · Node 24 · Browser acceptance · Chromium).

**Overall: `release_ready_with_external_qualification_pending`.** Every M5 track is finished and verified except the
real-model qualification, which cannot run here because the credential it needs does not exist on this machine.
That is a prerequisite gap, not a failure — and it is emphatically not a pass.

This document is the single exit for the M5 body of work. It replaces "read four rounds of HANDOFF to work out what is
closed", and `release-readiness.json` next to it carries the same facts in machine-readable form (the two are checked
against each other by `scripts/m5_release_readiness.py`).

## Status vocabulary

| Status | Meaning |
|---|---|
| **CLOSED** | finished, verified, reproducible; reopening has a written trigger |
| **ACCEPTED** | implemented and live-verified, admitted to the integrated line by a release ruling |
| **BLOCKED — EXTERNAL PROVIDER DEPENDENCY** | cannot run here because a prerequisite outside this repository is absent; neither a failure nor a pass |

## Track summary

| Track | Status | Anchor | Commits | CI | Visual |
|---|---|---|---|---|---|
| M5 deterministic self-repair benchmark v3 | **CLOSED** | `5fc0964` | `1ba141c`, `f60693d`, `acd0d94`, `5fc0964` | 35579988999, 35702397038 success | 35580046077, 35703003401 success |
| A5 corpus identity closeout | **CLOSED** | `5fc0964` | `f60693d`, `acd0d94`, `5fc0964` | 35702397038 success | 35703003401 success |
| B retry-contract integrity + first-attempt guard | **CLOSED** | `263808f` | `263808f` | 35706204970 success | 35706717144 success |
| TypeSafe judgment-based drawing | **ACCEPTED** | `72f364e` | `eabb58c`, `50653a5`, `7ce1395`, `72f364e` | 35707486170 success (35706798185 is the failure that was fixed) | 35707535636 success |
| A6 qualification readiness | **CLOSED** | `a92044f` | `a92044f` | 35708328119 success | — |
| A6 real-model qualification | **BLOCKED — EXTERNAL PROVIDER DEPENDENCY** | — | — | — | — |

## Identities (the numbers a reviewer re-derives)

| Identity | Value |
|---|---|
| `spec_version` | `3` (fingerprint `8f522c758b67fc71e53d7d382f1c09fc3d922663ea500a7fc09106ece054c810`) |
| `corpus_version` | `3` |
| `core_corpus_digest` | `96b999fa90403c82b58018ef82b06bb190932d97a66f179ebde2c0e2ef41c10f` |
| `oracle_version` / `semantic_hash_version` | `1` / `1` |
| corpus shape | 72 acceptance cases · 24 dev cases · 23 operators · 20 operators carrying a target code · 17 distinct target codes |
| archived v2 | `spec_version 2` (`c8520c5e7b0b1d7e77e3752080393b4965aee60538ad71a6bf95d5fc4ab5ede1`), `core_corpus_digest edb1c5d385bfd4f13a195119444dcb157f3bf0d893afd35e595ddc28c4b1c36c` |

Measured on the frozen deterministic track: acceptance 72/72, S@5 overall 1.0, every family's S@5 1.0, `gate_failures []`,
safety 13/13, and `pytest` 905 passed on CPython 3.11 in CI. `S@1` is published as `0.8333` and is **derived**, not
asserted as a constant: it equals the share of cases with no retry contract (60/72), because the other 12 are F6's
declared injections.

## What each track actually claims

### M5 deterministic self-repair benchmark — CLOSED

72 acceptance cases across six families, the success oracle, exactly one governed write per repair, the undo/redo pair,
the safety-negative suite and the frozen thresholds. Evidence: `reports/m5/repair-benchmark-acceptance.json` (the
historical v2 set), `reports/m5/repair-benchmark-summary.json`, `reports/m5-promotion/acceptance-v3.json`,
`reports/m5-promotion/acceptance-v3-summary.json`, `reports/m5-promotion/spec-projection.txt`,
`reports/m5-promotion/case-projection.txt`, `reports/m5-promotion/disposition-ledger.json`,
`reports/m5-promotion/disposition-ledger-summary.json`, `reports/m5/repair-coverage-extension.json`.

Committed by `1ba141c`, `f60693d`, `acd0d94` and `5fc0964`; the frozen deterministic gate is the one the whole M5
release rests on, so its evidence is the widest set in this record.

Reopening triggers: a version axis changes; a family's S@5 or F6's own S@5 drops below threshold; a no-retry case stops
succeeding first try; an F6 case stops converging on its declared attempt; the safety suite stops passing in full.

### A5 corpus identity closeout — CLOSED

The corpus is identified by a projection of its case universe and operator catalogue rather than by counts, the
projection is interpreter-independent (no environment-derived field inside it), and the archived v2 identity is still
recomputable. Committed by `f60693d`, `acd0d94` and `5fc0964`. Evidence: `reports/m5-closeout/corpus-identity.json`,
`reports/m5-closeout/corpus-identity.txt`, `reports/m5-closeout/corpus-identity-inputs.json`,
`reports/m5-closeout/corpus-identity-python3.11.txt`, `reports/m5-closeout/corpus-identity-python3.12.txt`.
Recorded 57 definition identities, 103 recomputed across two source files, identical on CPython 3.11 (in CI) and 3.12.

Reopening triggers: the same source disagrees across interpreters; the archived v2 body or rotation stops being
derivable; a definition in the identity stops being name-addressable.

### B retry-contract integrity — CLOSED

Every first-attempt failure is declared by the frozen spec, and the converse is checked too: a case that declares no
retry must land on its first attempt. Only F6 may declare one, its declared count must equal `F6_CONTROL_FLOW`, and it
must converge on exactly that attempt. 12 F6 cases (2/3/5 attempts, four each); zero first-attempt failures outside F6.
Committed by `263808f`.
Evidence: `backend/tests/test_repair_contract.py` (with a synthetic counterexample proving the check bites).

### TypeSafe judgment-based drawing — ACCEPTED

The agent panel configures a TypeSafe key and **reports where the key comes from without ever returning it**: green when
the server environment already holds one ("leave the field empty and it is used"), amber when nothing does. A key typed
into the panel lives in `sessionStorage` only, and a key travels in the request body only. Drawing is judgment-based:
code builds the candidate set, System One answers which candidate is meant, and a clause below the confidence floor
0.34 is skipped and reported rather than guessed.

Live verification is **manual by design** (CI has no credential and makes no paid external call): the drawing script
runs a real judgment (`noul 0.97`, 778 ms) and turns a sentence into a compiled transaction (2 judgments, both at
confidence 1.00, `valid=True issues=none`), and the Playwright panel check drives a real server with an empty key field
(`Key 有效 · jev-1.13.0`). Committed by `eabb58c`, `50653a5`, `7ce1395` and `72f364e`. Evidence:
`reports/typesafe/live-acceptance.txt`, `docs/typesafe-drawing.md`,
`backend/tests/test_typesafe_drawing.py`, `backend/tests/test_typesafe_credential_hygiene.py`,
`frontend/e2e/typesafe-panel.spec.ts`, `frontend/tests/typesafeSettings.test.ts`,
`frontend/scripts/typesafe-panel-check.mjs`, `scripts/typesafe_live_acceptance.py`.

One CI run in this range failed and stays in the record: **35706798185** failed because a new field label contained an
existing field's accessible name, so a selector by role and name resolved to two elements. The fix (`72f364e`) renamed
the fields, added four hermetic browser tests for the block, and the mutation check (renaming the label back) proves the
regression bites.

### A6 qualification readiness — CLOSED

The contract: an OpenAI-compatible endpoint, `PID_AGENT_LLM_BASE_URL` + `PID_AGENT_LLM_MODEL` both required
(`PID_AGENT_LLM_API_KEY` optional), 24 dev cases, thresholds `model_s5_overall ≥ 0.80` and `model_family_min ≥ 0.5`, and
exit codes 0 qualified / 2 ran but did not qualify / 3 prerequisite missing. Everything else is present: the suite
generates 24 cases, the oracle/orchestrator/write-governance/safety paths are covered by CI, and with the two variables
set to a loopback probe (no call made) `ModelRepairPlanner` builds with a full identity
(`prompt_fingerprint db205459aa542200…`, `schema_fingerprint f2b11350b0dcded1…`). Committed by `a92044f`. Evidence:
`reports/m5-qualification/readiness.md`, `reports/m5-qualification/readiness.json`.

### A6 real-model qualification — BLOCKED — EXTERNAL PROVIDER DEPENDENCY

`status: awaiting_real_model_qualification`, **exit code 3**, `qualified: false`, reason:
*"no model provider is configured: set PID_AGENT_LLM_BASE_URL and PID_AGENT_LLM_MODEL (CI has no external provider by
design)"*. The credential scan on this machine found only TypeSafe's key (which is a judgment model for the drawing
path and was **not** repurposed), no `PID_AGENT_LLM_*` / `AGENTCAD_LLM_*` anywhere, and no local OpenAI-compatible
server listening. Reopening trigger: a real repair-agent provider is configured, after which the frozen 24-case suite
runs unmodified and the result is read as `0 → QUALIFIED`, `2 → REAL MODEL EVALUATED BUT NOT QUALIFIED` (a candidate
finding, not something to explain away), `3 → PREREQUISITE MISSING`.

## Frozen evidence

`reports/m5`, `reports/m5-promotion` and `reports/m5-closeout` are byte-identical to their published state: nothing in
this exercise rewrote them. New evidence lives in `reports/m5-qualification/`, `reports/m5-release-readiness/` and
`reports/typesafe/`.

## Claims this document does not make

- the real-model qualification has **not** been run: no model was evaluated, so the candidate is **not** model-qualified;
- exit code 3 is a **missing prerequisite**, not a pass and not a rejected candidate;
- the TypeSafe System One model is not a repair-agent provider and was not used as one;
- no threshold, corpus, oracle, prompt contract or family denominator was adjusted to reach any status above;
- the v2 acceptance evidence under `reports/m5` is historical: v3 is the active identity and the archived v2 identity is
  recomputable but is not in force.
