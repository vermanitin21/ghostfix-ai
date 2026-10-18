# Patch Proposal Reliability v2

This changes the model-to-tool protocol, not the frozen corpus or verification
authority. Benchmark v1 remains **1/7 verified**. No Benchmark v2, live model
experiment or Docker corpus reference run is part of this milestone.

## Findings and diagnosis response

The old client used 4,096 output tokens for both stages. It disabled thinking
only for multiline proposals. Diagnosis used strict JSON schema without that
option. The recorded failure is an explicit API finish reason of length, caught
before JSON validation; the retained evidence does not expose token allocation,
so thinking consumption is a plausible contributor, not a measured causal split.

Diagnosis now uses the same explicit thinking-disabled option, keeps JSON schema
mode, and has a 2,048-token output cap. Proposal mode retains the established
JSON-object/newline workaround, disabled thinking and 4,096-token cap. SDK retries
remain zero; existing per-call timeouts remain unchanged.

The wire-only ConciseDiagnosis schema limits summary/action to 240 characters,
hypothesis to 400, evidence to three 240-character items, files to four bounded
names and symbols to four bounded names. The prompt requests short decision-useful
conclusions, not reasoning transcripts. Archival Diagnosis keeps its previous
bounds so old evidence remains readable.

Only the typed OutputBudgetExceeded exception, raised for explicit finish_reason
length, permits one diagnosis protocol retry. The retry uses identical serialized
repository/failure evidence with an additional concision instruction. Other
schema errors, refusals, API failures, timeouts or matching error-message text
do not authorize retry. Each call is recorded as started, accepted, output_budget
or error; no response reasoning transcript is stored.

## Unified diff contract and strict boundary

Proposal and correction prompts share a tiny generic example and require:

- Exactly one syntax prefix per hunk body line: space for context, plus for
  addition, minus for removal, followed by original text/indentation.
- Accurate original/new coordinates and counts, LF termination, no Markdown fences.
- Only supplied ordinary source paths; no tests, configuration, creation/deletion,
  renaming, dependency changes or unrelated changes.

The existing parser does not support the standard backslash no-newline marker.
The prompt explicitly rejects that unsupported form rather than adding a permissive
grammar. The response field remains unified_diff, matching the existing schema.

Test modification is now also rejected at proposal validation, not only at the
application boundary. Existing multi-file parser tests now use two source modules
rather than modifying a test file.

## Source-proven normalization

normalize_context_prefixes first runs ordinary strict validation. A valid proposal
is returned unchanged. Otherwise, a private pass through the same bounded parser
may insert exactly one space before an unprefixed hunk line only when:

1. Its first character is not already space, plus or minus.
2. Its complete text, including newline, equals the original source line at the
   declared hunk position plus the number of old-side lines consumed.
3. The whole resulting patch satisfies path policy, source matching, hunk
   coordinates/counts, nonoverlap, limits and non-no-op requirements.
4. A separate public strict validation pass also succeeds.

There is no search or fuzzy matching. Duplicate lines are acceptable only where
the explicit position proves the match. Existing prefixes and indentation are
never reinterpreted. No source text, additions, removals, paths, coordinates or
counts are generated or repaired. Partial recovery is discarded if any later
file or hunk fails. The application boundary still independently revalidates.

Missing edits, contradictory context, mismatched source/counts/coordinates,
unsafe files and unsupported operations remain rejected. A syntactically valid
normalized patch is not a verified repair; Docker tests remain authoritative.

## Narrow format retry

The retry allowlist is intentionally narrower than all malformed diffs:

- Exactly one enclosing Markdown diff fence around an otherwise strictly valid diff.
- A missing final LF on an otherwise strictly valid diff.

A local, strict-valid witness establishes the complete original intended effect.
It is never accepted, applied or executed as the candidate. If the witness cannot
be proven for the entire patch, there is no format retry. In particular, malformed
hunk counts or missing edits are not guessed.

At most one model format retry is allowed per proposal lifecycle, including a
proposal made during ordinary self-correction. The retry receives the prior
proposal, at most two bounded local validator messages plus the format category,
original files, and limits. It receives no structured diagnosis, sandbox logs or
targeted/regression verification feedback. Test source/selectors are allowed
context, not execution evidence.

The model must return the same intended repair with valid syntax. The result
passes normal normalization/strict validation and must produce the exact same
replacement file contents and test selectors as the witness. Semantic changes
are rejected even if individually valid. A second invalid output ends proposal
generation. Infrastructure and JSON-schema failures are not arbitrarily retried.

This deliberately leaves some structurally broken proposals unrecoverable.
Reliability never takes precedence over proving intent.

## Failure categories

PatchValidationResult has an additive failure_category:

- RETRYABLE_FORMAT_ERROR: only after a whole-patch witness proves the allowlisted
  envelope/final-LF defect.
- TEST_MODIFICATION: test targets.
- DISALLOWED_PATH: traversal, outside/unsupported context paths and target mismatch.
- SECURITY_POLICY_ERROR: sensitive/binary content, bounds and policy constraints.
- UNSUPPORTED_OPERATION: creation/deletion, renames, mode/configuration operations.
- SEMANTICALLY_AMBIGUOUS_PATCH: uncertain hunk/source/count interpretation or changed retry intent.
- OTHER_INVALID_PROPOSAL: remaining failures.

Static classification alone never authorizes a retry. The whole-patch witness is
mandatory, so an early format error cannot conceal a later unsafe header.

## Provenance and attempt semantics

DiagnosisAttempt adds protocol_retry_count and protocol_events. PatchProposalAttempt
adds its own bounded count and PatchProtocolEvent history containing response
SHA-256, model ID, raw model diff, normalized diff/reason when applicable and each
validation result. Sensitive strings and absolute host paths in new proposal
provenance are sanitized; the hash binds the original response. The final proposal
contains the candidate used for validation, while event history distinguishes
model syntax from deterministic normalization.

Both complete proposal responses are distinguishable if a retry returns them.
For a transport/schema exception that yields no usable response, the existing
safe error plus retry count records failure; unavailable raw content is not
invented. All new fields default safely when reading historical evidence.

RepairLoop and its maximum two candidate attempts are unchanged. A format retry
is inside one PatchProposalAttempt, not Repair Attempt 2. Diagnosis retries do not
create candidates. Maximum model calls with every permitted path exercised:
two diagnosis calls plus two calls for each of two repair candidates = six.

BenchmarkRun metrics are unchanged. First-attempt success still describes the
candidate lifecycle, not one model call. Future measurements must disclose the
nested protocol counters/events alongside those metrics; otherwise they would
hide extra calls. This milestone does not rewrite previous metrics or artifacts.

## Deterministic verification and limits

Synthetic tests cover exact/mismatched/duplicate context, missing edits, source
contradictions, multiple hunks, hidden unsafe headers, limits, security violations,
retry exhaustion, same-intent enforcement, concise diagnosis, provenance and
repair-attempt counts. Network/model responses and verification are mocked.

Optional offline replay of the five recorded v1 invalid proposals:

| Case | Syntax-only result |
| --- | --- |
| configured limit | Exact missing context prefix normalized; strict validation passes |
| pagination | Exact missing context prefix normalized; strict validation passes |
| shipping eligibility | Exact missing context prefix normalized; strict validation passes |
| order total | Rejected: malformed/incomplete hunk; no retry witness |
| port parsing | Rejected: malformed/incomplete hunk; no retry witness |

Replay reads only sanitized evidence, uses no reference fixes, makes no model or
Docker calls and does not apply patches. These results do not establish repair
correctness or a Benchmark v2 score. V1 remains unchanged.

Live behavior of the revised diagnosis settings/prompts and actual model format
retry compliance remains unverified pending review and a separately authorized
future run.
