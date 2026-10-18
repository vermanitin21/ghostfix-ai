# Hackathon demo: verified self-correction

This demo shows that GhostFix does not trust generated code. A wrong candidate
fails real execution. The failure becomes evidence for a real Nemotron correction,
and the corrected candidate must pass targeted and regression tests before a
Proof of Fix can say VERIFIED.

## Prerequisites

- Docker running Linux containers, with the `ghostfix-pytest-sandbox` image built
  (see [sandbox.md](sandbox.md)).
- `.env` containing `NEBIUS_API_KEY`, `NEBIUS_BASE_URL` and
  `NEBIUS_MODEL=nvidia/Nemotron-3_5-Lightning`.
- The project virtual environment.

## Command

```powershell
.\.venv\Scripts\python.exe scripts/run_self_correction_demo.py
```

`-m scripts.run_self_correction_demo` also works. Evidence goes to
`artifacts/hackathon-demo/` (gitignored). The command refuses to start if that
directory already exists, and it never overwrites earlier evidence. Use
`--output <new-dir>` for another run.

Before any model or Docker call, the command checks the model ID, the API key,
the output directory and the sandbox image. It prints a summary derived from the
actual run. It exits 0 only if self-correction was demonstrated and verified.

## Flow

```
broken calculator (return left - right)
  -> Docker reproduction: targeted test fails
  -> Nemotron diagnosis (model hypothesis)
  -> Candidate 1: DEMO-INJECTED return left * right (valid patch, wrong behavior)
  -> fresh copy of ORIGINAL source -> Docker targeted test FAILS (6 != 5)
  -> bounded execution evidence -> Nemotron correction request
  -> Candidate 2 (real model output) -> fresh copy of ORIGINAL source
  -> targeted PASS -> regression PASS -> integrity + cleanup
  -> Proof of Fix: VERIFIED
```

Every candidate goes through the normal strict validator and `PatchVerifier`.
Candidate 2 is never layered on Candidate 1. Each attempt gets its own temporary
copy of the original fixture. The evidence confirms this: the applied diff for
Candidate 2 removes the original line, not Candidate 1's line.

## Demo-injection safety

- Injection lives only in `scripts/test_self_correction.py` (`DemoAgent`, a
  script-only subclass). Production `GhostFixOrchestrator`, `RepairLoop` and the
  benchmark runner have no injection parameter, and nothing under `app/` imports
  from `scripts/`. Tests check both.
- The injected candidate is labelled `demo:injected-not-LLM`. Its summary starts
  with `DEMO:`, and it appears as DEMO-INJECTED in `demo.md` and
  `run-metadata.json`.
- Only Candidate 1 is injected. Diagnosis and correction are real Nemotron calls.
  The correction prompt and evidence bounds are the production ones.
- Docker establishes Candidate 1's failure. Nothing is simulated or marked failed
  by hand.

## Evidence produced

| File | Contents |
| --- | --- |
| `demo.md` | Judge-facing one-page summary |
| `demo-summary.json` | Stage-by-stage projection for a future UI: attempts, correction, integrity |
| `repair-run.json` | The complete sanitized `RepairRun` |
| `proof-of-fix.json` / `.md` | The existing deterministic Proof of Fix built from that `RepairRun` |
| `model-audit.json` | Every model call: stage, request hash, SDK options, raw response with exact SHA-256 |
| `run-metadata.json` | Commit, injection scope, call counts, fixture hashes, corpus fingerprint before and after, cleanup |

Raw-response fidelity follows the benchmark policy (`scripts/evidence_fidelity.py`,
a verbatim port of the benchmark audit policy). Safe text is stored exactly, with
its SHA-256. Text containing credentials or host paths is withheld, and its hash
and the reason are recorded. Every file passes a credential and host-path gate
before anything is written.

## Model hypothesis vs executed proof

The root cause in `demo.md` is labelled **model hypothesis**. Only executed
results count as proof: Docker reproduction, targeted and regression pytest exit
codes, the original-source hashes, and workspace cleanup.

## Demo vs benchmark

The demo uses a deliberately injected first candidate on one fixture. It is not
benchmark evidence and is not counted in any score. The locked benchmark results
use the same frozen seven-case corpus, with one official run per version:

| Version | Verified |
| --- | --- |
| v1 | 1/7 = 14.29% |
| v2 | 3/7 = 42.86% |
| v3 | 4/7 = 57.14% |

The corpus is small and model output is stochastic. These are engineering
evidence, not statistical proof.

## Limitations

- The demo covers one small Python fixture. Passing its tests is not formal
  correctness.
- Candidate 2 is real, stochastic model output. A canonical run can legitimately
  fail; if it does, the evidence keeps that result.
- The correction budget is two candidates in total.
- The correction request hash is an audit correlation value, not a remote
  attestation.
