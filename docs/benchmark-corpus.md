# GhostFix Benchmark Corpus v1

## Purpose and freeze policy

Seven small, standalone Python/pytest fixtures exercise distinct behavior rather than seven variations of arithmetic replacement. Difficulty (2 easy, 3 medium, 2 harder) is a human-authored relative label, not a verification input or a measured model result. Even the harder cases are deliberately small.

Freeze the manifest and model-visible fixture contents before any real model benchmark. Record the fingerprint printed by the validation command with the reviewed commit. No model was used to design, diagnose, propose repairs for, or tune these fixtures. Changes after review require a new fingerprint/version and must not be silently tuned against model outcomes.

Reference fixes are used only to validate benchmark solvability and are never used to score GhostFix.

Real benchmark results must be reported even when GhostFix fails a case.

## Inventory

All paths below are relative to the approved `fixtures/` root. Every case has exactly one failing target test function plus passing regression tests. Tests contain no parameterized node IDs, timing, network, randomness or external filesystem requirements.

| Case ID | Fixture / source files | Category | Difficulty | Target selector | Passing regression tests |
| --- | --- | --- | --- | --- | ---: |
| python-active-users-001 | active_users/users.py | collection | medium | test_users.py::test_active_record_after_inactive_record | 4 |
| python-calculator-add-001 | broken_math/calculator.py | logic-operator | easy | test_calculator.py::test_adds_two_numbers | 3 |
| python-configured-limit-001 | configured_limit/limits.py | error-handling | harder | test_limits.py::test_invalid_supplied_limit_propagates | 4 |
| python-order-total-001 | order_total/orders.py, pricing.py | integration-regression | harder | test_orders.py::test_discount_preserves_shipping_eligibility | 4 |
| python-pagination-001 | boundary_page/pagination.py | boundary | easy | test_pagination.py::test_complete_page | 3 |
| python-parse-port-001 | parse_port/ports.py | validation | medium | test_ports.py::test_non_integer_notation_rejected | 4 |
| python-shipping-eligibility-001 | shipping_eligibility/eligibility.py | conditional | medium | test_eligibility.py::test_independent_qualifications | 3 |

Each fixture contains the listed source and one matching `test_*.py` file; order_total contains two source files and one test file. All remain within the existing four-file / bounded-text context profile. No installation or new dependency is required.

### Intended behaviors and regression coverage (developer-facing)

- Calculator: integer addition; regression tests retain positive/negative right-identity and zero behavior. The old author hint was removed from its docstring.
- Pagination: complete one-based pages; empty input, beyond-end pages and invalid bounds remain correct.
- Eligibility: membership or sufficient order value qualifies; both qualifications, neither qualification and negative totals are covered.
- Active users: ignore inactive records when establishing first-active deduplication order; retain active duplicates, inactive-only, ordered and empty behavior.
- Port parsing: reject fractional/exponent notation rather than silently converting it; preserve integer boundaries, whitespace, invalid ranges and nonnumeric rejection.
- Configuration limit: distinguish missing values (default) from invalid supplied values (propagate conversion failure); numeric strings, zero and incompatible types are covered.
- Order total: apply a discount to the amount payable while assessing shipping eligibility from the original subtotal. Both source files must be understood: pricing computes the subtotal and discounted amount, while orders decides shipping.

### Regression trap

For order_total, the broken implementation assesses free shipping on the discounted amount. The target order has subtotal 60, discount 20 and payable amount 40; shipping should remain free.

An obvious target-only change is to lower the discounted-total shipping threshold from 50 to 40. That makes the target pass but wrongly gives free shipping to an undiscounted order of 45. `test_below_shipping_boundary` catches this. The human reference preserves the threshold and assesses eligibility from the pre-discount subtotal instead.

This is a legitimate domain distinction, not a model-specific trick. The explanation and naive patch are not part of model-visible fixture context. Fixture docstrings describe contracts, not repair instructions.

## Reference isolation and context hygiene

Human-authored unified diffs live in `benchmarks/reference_fixes/<case-id>.diff`, outside `fixtures/`. The QA tool alone loads them. There are no fixed modules, answer files or reference diffs within fixture trees.

Normal repair context uses the existing `pipeline_repair` adapter: it constructs a bounded fixture snapshot, passes only those relative source/test paths to analysis, and subsequently calls the existing repair loop. It does not read this document, the reference directory, or QA artifacts. Unit tests capture those exact adapter arguments for all seven cases, inspect all context file contents and reject traversal into reference fixes.

No case-ID, fixture-name or target-selector branches were added to production repair modules. A source inspection test enforces that boundary. The evaluation engine, repair orchestrator, sandbox, model client and verifier remain unchanged.

The old evaluation CLI unit smoke test now selects the calculator explicitly because its frozen synthetic adapter only models that case. The generic mock CLI is not a seven-case solvability check; use corpus QA for that purpose.

## Validation architecture

`app/evaluation/corpus.py` is distinct from performance evaluation. `CorpusCaseValidation` records normal broken reproduction, reference application, targeted/regression pass flags, exact original integrity, temporary workspace cleanup and bounded error categories. `CorpusValidationRun` aggregates those results with purpose `human-reference-corpus-qa`, counts and fingerprint. No model provenance or repair-success-rate claim is manufactured.

For each enabled case:

1. Validate metadata, path containment, selectors and existing context budgets.
2. Hash every model-visible source/test file's exact bytes.
3. Execute the entire broken suite through the existing restricted Docker runner. Require pytest exit 1, no timeout/infrastructure error, and exactly the expected failed node IDs. Other failures invalidate the case.
4. Read the isolated, bounded, nonlinked human reference. Wrap it as a human-labeled candidate and use the existing pure patch validator.
5. Pass the candidate to the existing PatchVerifier, which applies it only in a temporary copy, checks the resulting diff, then runs target tests and the complete suite in Docker.
6. Confirm the original exact-byte hashes remain identical and the verifier confirms workspace cleanup.
7. Record the result. No raw command, mount path, stdout, exception message or credential is serialized.

Fixtures execute only in Docker: networking disabled, non-root, read-only root and repository bind mount, dropped capabilities, no-new-privileges, CPU/memory/PID bounds, writable bounded tmpfs and timeout. Temporary patch copies and container lifecycle remain owned by the existing verifier and sandbox, respectively. Reference validation does not bypass those boundaries.

Disabled cases are not executed or included in QA counts; their model-visible contents still contribute to the registered corpus fingerprint. Failures are reported without editing the source fixture. Lack of confirmed reproduction, integrity, cleanup or fingerprint causes a nonzero CLI exit. If no cases are enabled the CLI also exits nonzero.

## Fingerprint algorithm

`corpus_fingerprint_sha256` is SHA-256 over UTF-8 compact canonical JSON:

- Object contains `manifest` and `files`.
- Manifest is the fully validated Pydantic representation, including defaults; cases sorted by stable ID.
- Files maps each registered case ID to its bounded model-visible snapshot: relative file names and complete text.
- CRLF and CR text line endings normalize to LF; other text is unchanged.
- JSON uses sorted keys, `ensure_ascii=False` and separators `(",", ":")`.

No timestamps, absolute paths, reference fixes, runtime artifacts or generated caches participate. Exact-byte integrity hashes are separate from this cross-platform normalized fingerprint. Tests demonstrate repeatability, ordering stability, change sensitivity for source/manifest changes, and exclusion of reference material. No optional lock file is generated this milestone; preserve the fingerprint with the reviewed commit and QA artifact.

## Commands

From repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_corpus.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.validate_corpus
```

The explicit validation script requires Docker and the existing configured sandbox image. It prints per-case results and the final fingerprint, and writes `artifacts/corpus-validation.json` (already gitignored). It never instantiates a model client. Ordinary unit tests use mocked sandbox evidence and real pure patch application in temporary copies; they do not execute fixtures on the host or need Docker/Nebius.

## Limitations

Seven small Python utility fixtures are not representative of real repositories, multi-language repair, dependency failures, or general software correctness. Tests define the exercised contract, not an exhaustive proof. Reference success establishes solvability under executed tests only. Difficulty labels are provisional human judgments; no real GhostFix performance has been measured. Freeze remains subject to corpus review.
