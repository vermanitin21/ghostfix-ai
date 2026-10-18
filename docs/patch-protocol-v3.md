# Patch Protocol Reliability v3: Deterministic Diff Recovery

This milestone changes only the model-to-patch boundary. The frozen corpus, the
strict validator and verification authority are unchanged. Benchmark v1 remains
**1/7 verified** and Benchmark v2 remains **3/7 verified** (run
`db2b2642-c67b-40b3-9fa4-62ba5321f6ba`). No benchmark, live model call or Docker
execution is part of this milestone.

## Representation recovery, not semantic repair

`app/agent/recovery.py` may change how a model's patch is *written*, never what
it *does*. It never invents, deletes, reorders or edits body text. It never
changes paths or hunk start coordinates, and it never searches source for a
better match. Each permitted change has to be proven by two things together:
the structure the model supplied, and the original source at the exact
expected position. If that proof fails, recovery rejects the patch.

## Pipeline

`recover_representation(proposal, context, root, limits)` is pure: it does no
file IO, runs nothing, and calls no model.

1. **Strict validation of the raw diff.** If it passes, the proposal is
   returned byte-for-byte unchanged with no transformations.
2. **Policy gate, before any transformation.** The gate scans the whole text.
   It checks byte and control-content limits and rejects Unicode line
   separators. It applies the strict path policy (traversal, sensitive, test
   and unsupported files) to declared targets and to every `---`, `+++` and
   `diff --git` line. It checks test selectors. It rejects git metadata
   operations (creation, deletion, mode, rename, copy, binary) and the
   `\ No newline at end of file` marker. A gate failure is reported with its
   policy category, and no transformation is attempted.
3. **Terminal-newline canonicalization** (transport layer).
4. **Line-by-line body walk** with source alignment and missing-context-prefix
   recovery.
5. **Per-hunk count-metadata recovery.**
6. **Strict validation of the recovered text** using the unchanged
   `validate_patch`. This is the final authority. If it fails, every
   transformation is discarded and the raw proposal and its raw failure are
   kept.
7. The orchestrator then applies its existing checks: secret and host-path
   provenance, repository stability and same-intent comparison.

The newline step comes before the body walk because the last body line cannot
be compared byte-for-byte with source until its line terminator exists. This
ordering is recorded in the provenance.

## Transformations

| Category | Allowed change | Proof required |
| --- | --- | --- |
| `TERMINAL_NEWLINE_CANONICALIZATION` | Append exactly one LF to transport text that lacks one | The whole result must then pass the walk and strict validation; the final line must still align or be an explicit addition |
| `MISSING_CONTEXT_PREFIX_RECOVERY` | Insert one space before an unprefixed body line | The complete line, including its LF, equals the source line at the exact cursor (declared start plus old-side lines consumed) |
| `HUNK_COUNT_METADATA_RECOVERY` | Rewrite `old_count`/`new_count` only | The rules below |

### Hunk-count metadata recovery

The body determines `old = context + removals` and `new = context + additions`.
The header counts may be replaced only when all of these hold:

- The body is non-empty and ends at an unambiguous boundary: the next `@@ `,
  file header, or end of text. A boundary line that could also be read as body
  text at the cursor is rejected as ambiguous.
- Every body line had an explicit `" "`, `"+"` or `"-"` prefix. Count recovery
  and prefix recovery are **never combined in one hunk** (see below).
- Every context and removal line equals source exactly, starting at the
  declared start. The start is never moved.
- The body contains *at least* the lines the header claims: `body_old >=
  header_old` and `body_new >= header_new`. If the header claims a line that
  is absent from the body, that line may be an omitted removal or addition. It
  is structurally indistinguishable from a miscount, so recovery rejects it.
- No count changes between zero and non-zero, because that would change what
  the start coordinate means.
- No body line is inserted, deleted or edited. Starts, paths and section text
  are kept as written.
- The rewritten patch passes the same strict validator. That includes
  next-hunk new-start consistency, so a later hunk whose `new_start` ignores
  the corrected delta is rejected rather than shifted.

**Why the two recoveries never combine.** An unprefixed line that equals
source could be context, or it could be a removal whose `-` was dropped. In
v2, the accurate header counts were the only evidence that told these apart.
For example, `@@ -1,2 +1,2 @@`, then ` def f():`, then an unprefixed `y = 1`,
then `+y = 2`. If both recoveries were allowed, this would become a strictly
valid *insertion* instead of the replacement the header describes. So a
prefix-recovered hunk must already match its header counts exactly.

### Terminal newline

Exactly one LF is appended only when the text lacks a final LF. The
`\ No newline at end of file` marker is a semantic marker, not a transport
defect. It is preserved verbatim and rejected by the policy gate, because the
strict subset does not support it. A trailing context line written as `" "`
is a real blank context line. It is kept only when source has a blank line at
the cursor. Otherwise the patch is rejected; the line is never stripped.

### Malformed bodies

This is not a general malformed-hunk repair system. An unprefixed line becomes
context only through complete-line equality at the exact cursor. Anything else
fails closed: Python-looking text, text matching source elsewhere, `*`-style
prefixes, trailing prose, stray headers, or an empty or truncated hunk. An
unprefixed *indented* line begins with a space, so it is read as context that
has lost one space of indentation. It then fails alignment, so prefix recovery
in practice only ever applies to unindented lines.

## Security invariants

- Path, test-file, unsupported-operation and secret policy are checked before
  any transformation and again by strict validation.
- No transformation can change a path, a hunk start, or added or removed text,
  or turn context into an edit or an edit into context.
- The secret and absolute-path checks on the final candidate happen after
  recovery, in the orchestrator.
- `patched_contents` and `PatchVerifier` revalidate the candidate strictly
  before applying it to a disposable copy.

## Provenance

`PatchProtocolEvent` gains additive, defaulted fields; historical events still
load:

- `raw_model_diff`: unchanged; always the model's own text.
- `normalized_diff`: the recovered text, only when a transformation was applied.
- `transformations`: an ordered list of `PatchTransformation` (`order`,
  `category`, `file`, `hunk`, `diff_line`, `detail`).
- `raw_validation`: the strict result of the raw diff.
- `validation`: the authoritative result for the candidate.
- `recovery_rejection`: why recovery did not apply.
- `normalization_reason`: still `source_proven_context_prefix` when only
  prefixes were restored, otherwise `deterministic_representation_recovery`.

`RepresentationRecovery.attempted` lists steps that were computed before a
rejection. These steps are discarded and are useful for audit only.

## Retry interaction and attempt accounting

Recovery runs before the existing narrow format-retry witness and never
broadens it. If recovery yields a strictly valid candidate, no model call is
spent. A missing final LF on an otherwise valid diff is now recovered
deterministically instead of costing the v2 retry. If recovery fails, the
retry eligibility is the existing one: a whole-patch witness for an exact
Markdown envelope or a missing final LF, with at most one retry. Security,
path, test-modification, unsupported-operation and ambiguity failures remain
zero-retry. A retried response goes through the same recovery and then the
existing same-intent check.

Recovery events are not repair attempts, Docker attempts, self-corrections or
protocol retries. `protocol_retry_count`, `RepairAttempt` numbering,
`RepairRun.max_attempts` and BenchmarkRun metrics are unchanged.

## Offline replay of the four Benchmark v2 invalid proposals

`artifacts/benchmark-v2-recovery-replay-v3/` (gitignored) holds the replay
script, `replay.json` and `replay.md`. The replay reads only the recorded v2
evidence, the manifest and model-visible fixture source. It disables network
and subprocess, and it reads no reference fixes. It executes nothing.

| Case | v2 failure | Recovery | Executable candidate |
| --- | --- | --- | --- |
| active users | Hunk line counts do not match | Counts `-12 +12` → `-12 +13` | yes |
| order total | Hunk line counts do not match | Counts `-8 +8` → `-8 +9` | yes |
| port parsing | Malformed or incomplete hunk body | Rejected: header claims a new line absent from the body (possible omitted addition) | no |
| shipping eligibility | Unified diff must end with a newline | Rejected: after the LF, the trailing `" "` context line is not proven by source | no |

That is 2/4 previously rejected proposals. **An executable candidate is not a
verified repair**: it has not been applied or tested, and it does not change
any benchmark score.

## Known limitations

- Count recovery trusts explicit body prefixes. A line the model mis-prefixed
  explicitly is taken as written; Docker verification decides whether the
  candidate works.
- Policy-gate scanning is conservative. A removal of source text beginning
  `-- ` or an addition beginning `++ ` looks like a header and stops recovery,
  though strict validation of a correctly counted raw diff is unaffected.
- When header counts are understated, the strict validator's raw error can be
  the generic header error rather than a count error. That is pre-existing
  wording.
- Live model behavior with this pipeline is unmeasured: no Benchmark v3 has
  been run. Prompts are unchanged; they already require valid prefixes,
  accurate counts and one final LF.
