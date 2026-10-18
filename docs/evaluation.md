# Deterministic evaluation

Benchmark success means GhostFix produced a candidate that satisfied its executed verification policy for that benchmark case.

It does **not** mean formal correctness. A one-case manifest is infrastructure validation, not evidence of broad repair reliability. Mock results are synthetic infrastructure tests, not measured model performance.

## Architecture and evidence

- `models.py`: strict, extra-field-forbidden, versioned case, manifest, result, metrics and run contracts.
- `evaluator.py`: pure `evaluate_case(case, repair_run, proof=None, *, secrets=())`. No IO, LLM, shell, Docker, test execution or mutation. Rebuilds Proof of Fix from the supplied run, even if a report is provided. Never trusts model confidence, prose, difficulty or category labels.
- `metrics.py`: pure aggregation of completed evidence, with explicit denominator/sample counts.
- `runner.py`: bounded orchestration and approved-root validation. Accepts an async `(case, RepositoryContext) -> RepairRun` dependency. `pipeline_repair(agent, repair_loop)` composes the existing `agent.analyze` and `RepairLoop.run`; no duplicated repair logic.
- `renderers.py`: typed, sorted-key JSON and Markdown derived from those same metrics.
- `safety.py`: output redaction; raw exceptions and environment variables are never exported.
- `mock.py`: explicitly synthetic, deterministic RepairRuns for offline infrastructure testing.

The frozen repair, sandbox, self-correction and Proof of Fix implementations are unchanged. A successful final candidate must satisfy the existing independently rebuilt Proof of Fix checks (raw test consistency, candidate linkage, patch effects, both test stages, original integrity and cleanup). The evaluator additionally requires a successful diagnosis and observed reproduction IDs covering the case's expected failing selectors. Missing extracted IDs are incomplete evidence, not guessed matches.

Per-case results retain the evidence run ID, freshly derived proof status, attempt statuses, evidence issues, model identities and supported metrics. They do not embed raw source files, logs or command mount paths. The caller retains the original RepairRun/ProofOfFix; an evidence run ID is a reference, not a claim that a separate proof artifact was persisted.

## Schemas and manifest

`BenchmarkCase`: stable lowercase hyphenated `id`, bounded `name` and `description`, relative `fixture_path`, `language="python"`, `framework="pytest"`, 1–8 `expected_failing_tests`, tags, enabled, optional difficulty/bug category. Manifest schema `1.0` contains a name and up to 100 cases with unique IDs. Unknown fields, commands, absolute/traversal paths, coerced booleans and unsupported languages are rejected. Difficulty/category/description never determine success.

`benchmarks/manifest.json` registers only `python-calculator-add-001`: Incorrect arithmetic operator, fixture `broken_math`, expected failure `test_calculator.py::test_adds_two_numbers`, category `logic`.

`BenchmarkCaseResult`: lifecycle status, verified flag, attempt count, first-success/retry/recovery flags, final targeted/regression pass observations, all-observed integrity/cleanup flags, failure category, retry-exhaustion flag, successful-patch counts, supported durations, model identities, evidence reference/proof status, attempt statuses/issues and skip reason. Missing observations are null, never invented passes or zeros.

`BenchmarkRun`: schema version, run ID, timestamp, benchmark name, caller-supplied optional 40-character baseline commit, explicit mock/pipeline mode, ordered case results, metrics, deduplicated models, limitations. No raw environment metadata. JSON round-trips through Pydantic; fixed inputs/ID/timestamp produce identical serialized output. Normal pipeline runs generate an ID and timestamp; mock CLI uses fixed values.

## Lifecycle and bounded execution

Cases are sorted by ID. Disabled cases, nonselected cases, cases beyond `max_cases` and cases skipped after a safety stop remain in the report with explicit reasons. `max_cases` is a strict integer from 1 to 100 and counts attempted case executions, including failed setup. Unknown/duplicate selections fail before execution. Default bound is 10.

- `COMPLETED`: verified success or a known terminal failure supported by returned evidence.
- `INCOMPLETE`: unfinished, missing, contradictory or unknown evidence; also exceptions before a usable RepairRun is returned.
- `SKIPPED`: no repair invoked; excluded from rate denominators.

Known safe test/proposal failures do not stop later cases. Explicit integrity/cleanup failure or an unhandled per-case exception stops further selected cases (`safety_stop`); existing results are preserved. An exception has no trustworthy cleanup evidence, so continuing would be unsafe. Exception strings are not serialized. Cancellation/interrupts propagate. The existing repair/verifier owns resource cleanup; the benchmark layer creates no containers/workspaces and cannot promise cleanup for arbitrary injected functions.

When a correction proposal fails before execution, RepairLoop retains the previous candidate's verification. The evaluator recognizes this exact linked pattern as `INVALID_PROPOSAL`, preserves earlier duration/attempt evidence, and leaves *final-candidate* test outcomes null. It does not attribute the previous candidate's test result to the unexecuted proposal. The rebuilt proof can correctly remain `INCOMPLETE_EVIDENCE` while the benchmark records this known terminal failure as completed.

## Failure taxonomy and precedence

One primary category is selected deterministically; `retry_budget_exhausted` separately preserves exhaustion without hiding the underlying failing stage.

1. `INTEGRITY_FAILURE`: any observed verification says original integrity failed.
2. `CLEANUP_FAILURE`: any observed verification says cleanup failed.
3. `TIMEOUT`: raw reproduction/test timeout or structured run timeout.
4. `INFRASTRUCTURE_FAILURE`: raw infrastructure error or structured run infrastructure failure.
5. `INCOMPLETE_EVIDENCE`: contradictory linkage, flags, tests, diff, order, diagnosis or case identity; missing required completion/evidence.
6. `REPRODUCTION_FAILURE`: original pytest failure not reproduced (without the higher-priority conditions).
7. `DIAGNOSIS_FAILURE`: reproduction succeeded but diagnosis did not.
8. `NONE`: independently verified success.
9. `INVALID_PROPOSAL`: final proposal absent, errored or failed local validation.
10. `PATCH_APPLICATION_FAILURE`, `TARGETED_TEST_FAILURE`, `REGRESSION_FAILURE`: mapped final executed stage.
11. `RETRY_BUDGET_EXHAUSTED`: fallback when the run explicitly records exhaustion but no more specific stage is available.
12. `UNKNOWN`: unmapped terminal evidence, treated as incomplete. Other insufficient evidence also maps to `INCOMPLETE_EVIDENCE`.

Higher-priority known safety failures can coexist with evidence issues; those issues remain visible. A missing completion timestamp always makes lifecycle status incomplete even if a specific safety category is available. Legitimate pre-verification failures do not require nonexistent final verification. Structured timeout/infra terminal signals do not require invented test results. A claimed success is never accepted merely from a status string.

## Metrics and exact denominators

Let C = completed cases, V = verified completed cases, R = completed cases with more than one recorded candidate attempt. A generated-but-invalid correction is an attempt; no unrecorded model activity is inferred. Retries *actually attempted*, not every initial failure that might benefit from retrying, form R.

| Metric | Numerator / denominator or sample |
| --- | --- |
| total_cases | All manifest results, including skipped |
| completed_cases / verified_cases | Counts C / V |
| failed_cases | C minus V (excludes incomplete) |
| incomplete_cases / skipped_cases | Respective lifecycle counts |
| repair_success_rate | V / C |
| first_attempt_success_rate | Verified cases with exactly one attempt / C |
| self_correction_usage_rate | R / C |
| self_correction_recovery_rate | Verified cases in R / R |
| targeted_pass_rate | True final targeted observations / completed cases with a consistent, observed final targeted outcome |
| regression_pass_rate | True final regression observations / completed cases with a consistent, observed final regression outcome |
| integrity_pass_rate | True all-observed integrity outcomes / completed cases with at least one verification observation |
| cleanup_pass_rate | True all-observed cleanup outcomes / completed cases with at least one verification observation |
| total/mean/median/max_attempts | Candidate attempt counts over C; total is their sum |
| mean_changed_files/additions/deletions/patch_size | Successful final patches in V with that field measured; patch size = additions + deletions |
| mean_reproduction_duration_ms | Recorded reproduction sandbox duration over C with a value |
| mean_targeted_duration_ms | Per-case sum of all observed targeted sandbox durations over C where targeted tests ran |
| mean_regression_duration_ms | Per-case sum of all observed regression sandbox durations over C where regression tests ran |
| mean_verification_duration_ms | Per-case sum of all observed targeted + regression sandbox durations over C where any test ran |
| failure_counts_by_category | Every nonverified, nonskipped result, including incomplete; zero entries retained; NONE excluded |

Incomplete/skipped cases contribute to lifecycle/failure counts but **not** rates, attempt statistics, patch or timing averages. Undefined rates/means/median/max are null, rendered N/A, never zero. A known duration of zero is retained. Every rate exports `rate_denominators`; every mean exports `mean_sample_counts`. Regression's denominator can be smaller than targeted's because targeted failures prevent regression execution. Therefore stage rates are conditional observations, **not** end-to-end repair rates. Inspect denominators and incomplete counts together with rates to avoid selection bias.

Timing values come from raw sandbox evidence, not model estimates. They include failed attempts. They exclude LLM latency, patch application, workspace setup and orchestration overhead. `end_to_end_duration_ms` remains null: RepairRun creation/completion does not reliably measure reproduction plus diagnosis and the entire pipeline.

### Exact mixed test arithmetic

Five completed synthetic cases: first-attempt success, first-attempt success, targeted failure then success, two targeted failures, regression failure. Attempt counts `[1,1,2,2,1]`.

- Repairs: 3/5 = 0.6; first attempt: 2/5 = 0.4.
- Retry use: 2/5 = 0.4; retry recovery: 1/2 = 0.5.
- Final targeted: 4/5 = 0.8; final regression: 3/4 = 0.75.
- Integrity/cleanup: 5/5 = 1.0 each.
- Attempts: total 7, mean 1.4, median 1, maximum 2.
- Successful patch averages: 1 file, 1 addition, 1 deletion, size 2 (three samples).
- Reproduction mean: 10 ms (five samples). Targeted totals `[20,20,40,40,20]`, mean 28 ms. Regression totals `[30,30,30,null,30]`, mean 30 ms (four samples). Verification totals `[50,50,70,40,50]`, mean 52 ms.
- Primary failures: one TARGETED_TEST_FAILURE and one REGRESSION_FAILURE. Exhaustion is also retained on the two unsuccessful results.

## Commands and artifacts

From repository root, PowerShell:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_benchmark --manifest benchmarks/manifest.json --mode mock
.\.venv\Scripts\python.exe -m pytest tests/test_evaluation.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Optional `--case python-calculator-add-001`, `--max-cases 1`, `--baseline-commit <40-character-commit>` are supported. CLI writes `artifacts/benchmark-results.json` and `artifacts/benchmark-results.md`, both already covered by `/artifacts/` in `.gitignore`. Exit 1 for failed/incomplete cases or bad input/output operations; exit 0 otherwise. Mock mode never calls Nebius or Docker, never executes fixture code, and labels synthetic model provenance explicitly. The single manifest demo uses one synthetic first-attempt success; the five-case mixture is a separate deterministic unit test, not five real fixtures.

No live CLI mode is enabled this milestone. Future callers can inject `pipeline_repair` with configured existing components and use `mode="pipeline"`. That adapter is tested with mocked components, not a live benchmark. Real networking, Docker restrictions and cleanup remain owned by the existing pipeline. No model judge, new retry policy, dependencies or arbitrary commands are introduced.

## Security and limitations

Fixture paths are relative to a caller-approved fixture root, resolved with containment checks; symlinks/Windows reparse points on fixture paths are rejected. Test selectors reuse existing `validate_test_ids` and RepositoryContext's bounded text/link/hardlink checks. The real adapter uses the existing bounded Python fixture snapshot (currently four small Python files), not whole arbitrary repositories. Manifest data cannot supply commands. Repositories remain untrusted; no Python fixture is imported or executed on the host.

Serialized output is an allowlist of normalized measurements and identifiers, not raw environment variables, logs, exceptions or mounts. Pass known credentials via `secrets` for exact redaction; sensitive assignment lines and host paths are also filtered. Unknown secrets cannot be recognized universally. Metadata must not contain credentials. Markdown escapes case identifiers and limitations; only typed values populate metric cells. Avoid hostile concurrent filesystem mutation: as in the existing fixture adapter, this is a small trusted-control-plane fixture harness, not a race-proof multi-tenant filesystem boundary.

Synthetic runs validate arithmetic, schema, evidence gating and integration wiring only. No live repair reliability, latency, model comparison or broad generalization is claimed. Optional CSV is intentionally omitted.
