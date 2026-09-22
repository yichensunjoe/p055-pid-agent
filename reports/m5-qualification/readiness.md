# A6 · real-model qualification readiness (M5, Track M)

**Verdict: `awaiting_real_model_qualification` — exit code 3, and a missing provider credential is the
only blocker.** No product code was written for this step, and no threshold, corpus or oracle value
was touched.

- Candidate: `72f364e` (= `origin/main`)
- Command: `.venv/bin/pid-agent repair-qualification --candidate-sha 72f364e --output reports/m5-qualification/readiness.json`
- Result: exit **3**, `status: awaiting_real_model_qualification`,
  `reasons: ["no model provider is configured: set PID_AGENT_LLM_BASE_URL and PID_AGENT_LLM_MODEL (CI has no external provider by design)"]`
  (machine-readable copy in `readiness.json`)

## 1. What the contract actually requires

Read from `backend/agentcad/repair_qualification.py`, `backend/agentcad/cli.py` and `.env.example`
rather than from memory:

| Item | Requirement |
|---|---|
| Provider interface | any **OpenAI-compatible** chat-completions endpoint (`ProviderConfig` → `ModelRepairPlanner`, `provider_class='openai-compatible'`) |
| Credential env vars | `PID_AGENT_LLM_BASE_URL` + `PID_AGENT_LLM_MODEL` (**both** required); `PID_AGENT_LLM_API_KEY` optional (a local server needs none). Aliases: `AGENTCAD_LLM_BASE_URL` / `AGENTCAD_LLM_MODEL` / `AGENTCAD_LLM_API_KEY` |
| Case set | `dev` suite = 4 cases per family × 6 families = **24**, verified by generating it: `{'F1': 4, 'F2': 4, 'F3': 4, 'F4': 4, 'F5': 4, 'F6': 4}` |
| Gates | `model_s5_overall ≥ 0.80`, `model_family_min ≥ 0.5` (i.e. ≥ 2 of 4 per family), safety smoke 100% safe (no write where the answer is a refusal) |
| Exit codes | `0` qualified · `2` ran but did not clear the thresholds (or evidence did not recompute) · `3` could not run: no usable provider |
| Evidence written when it can run | `<database>.evidence.json` plus `--output` report |

The interesting property of this contract is that exit 3 is **not** a failure of the candidate: a
missing credential must look neither like a pass nor like a rejected candidate, which is why the
status is `awaiting_real_model_qualification` rather than `not_qualified`.

## 2. Credential scan on this machine (names only, never values)

| Location | Result |
|---|---|
| `~/.zshrc`, `~/.zshenv` | only `TYPESAFE_API_KEY` / `TYPESAFE_BASE_URL` |
| `~/.zprofile`, `~/.bash_profile`, `~/.bashrc`, `~/.profile`, `~/.config/fish/config.fish`, `~/.env`, `~/.envrc` | nothing |
| current shell environment | nothing |
| `launchctl getenv` | not set |
| project `.env*` | only `.env.example` (all values commented out) |
| local OpenAI-compatible servers | none listening: no Ollama (11434), no LM Studio (1234); port 5000 is macOS ControlCenter (AirPlay) |
| `~/.workbuddy-ai/mcp.json`, `~/.claude.json`, `~/.codex/config.toml` | no `PID_AGENT_*` / `AGENTCAD_*` entries |

So the project's own qualification credential does **not** exist on this machine. The TypeSafe key
that does exist is a different thing entirely — a System One *judgment* model used by the drawing
path — and per the release ruling it was **not** repurposed as a repair-agent provider.

## 3. Everything else the run needs is present

Verified, not assumed:

- the 24-case suite generates with the frozen layout (§1);
- the planner that would be used exists and carries a real identity — with the two variables set to a
  probe endpoint (loopback, no call made), `configured_provider()` returns the config and
  `ModelRepairPlanner` builds:
  `planner_id='model-repair-planner' planner_version='1' provider_class='openai-compatible' base_url_class='loopback' prompt_fingerprint='db205459aa542200…' schema_fingerprint='f2b11350b0dcded1…'`.
  In other words, the only thing standing between this command and a real qualification run is the
  endpoint itself;
- the oracle, the orchestrator, the one-governed-write rule, the safety smoke and the evidence
  verifier are all exercised in CI: `pytest` **905 passed** on CPython 3.11 (run `35707486170`), and
  the model-track request shape is covered offline by `tests/test_repair_model_planner.py`;
- the deterministic half of M5 stays green and unchanged: acceptance 72/72, six families S@5 = 1.0,
  `gate_failures []`, safety 13/13, `core_corpus_digest 96b999fa…`; `reports/m5/**` written into by
  nothing in this step.

## 4. What happens the moment a credential exists

```bash
.venv/bin/pid-agent repair-qualification \
  --candidate-sha <HEAD> \
  --database /tmp/repair-qualification.db \
  --output reports/m5-qualification/qualification.json
```

That writes `reports/m5-qualification/qualification.json` (the verdict, quoted from the evidence)
and `/tmp/repair-qualification.db.evidence.json` (the recomputable per-case evidence, to be copied
into `reports/m5-qualification/` rather than left in a temporary directory). Exit 0 means a real
model qualified; exit 2 means it ran and the candidate did not clear the thresholds, which would be
a candidate finding and not something to explain away.

## 5. Not done, on purpose

- no TypeSafe credential used as a repair-agent provider;
- no mock or fake transport used to produce a "qualification" result;
- no change to thresholds, corpus, oracle or version axes;
- no UI work for the F6 retry fixtures (release ruling: benchmark diagnosis, already contracted in
  the spec and reports).
