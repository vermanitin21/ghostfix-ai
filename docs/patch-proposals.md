# Candidate proposals (no application)

Run from the repository root with Docker and the existing Nebius configuration:

```powershell
.\.venv\Scripts\python.exe -m scripts.test_patch_proposal
```

This reproduces the original fixture, requests one diagnosis and one candidate
from `nvidia/Nemotron-3_5-Lightning`, validates the candidate, prints a summary and
diff, and verifies fixture file hashes are unchanged. Success requires a valid
proposal targeting `calculator.py`. No patch is applied or executed. A candidate
is NOT a verified repair; behavior and regressions remain untested.

`GhostFixOrchestrator.propose_patch(diagnosis_attempt, repository, limits)` returns
`PatchProposalAttempt`, retaining the entire diagnosis attempt (including original
sandbox evidence and context), schema-valid proposal even when locally rejected,
`PatchValidationResult`, model identifier, timestamps, and safe errors. Records
are JSON-serializable in memory; no database or automatic disk persistence is added.
Malformed model JSON yields a failed attempt without retaining unsafe raw output.

`PatchProposal` records target files, summary, rationale, unified diff, expected
behavior, tests to run, risk notes, and confidence. `PatchLimits` defaults to two
files, 100 changed lines (additions plus deletions), and 12,000 UTF-8 diff bytes.
The script reads `PATCH_MAX_FILES`, `PATCH_MAX_CHANGED_LINES`, and
`PATCH_MAX_DIFF_BYTES` from existing settings; callers can pass an explicit policy.

## Validation

`validate_patch()` in `app/agent/patches.py` is pure: it reads no filesystem, runs
no commands, and writes nothing. It checks approved-root lexical containment,
exact context membership, canonical relative paths, and exact correspondence
between declared and parsed targets. It rejects hidden metadata, environment,
secret, lock/dependency/configuration paths, generated/vendor directories, and
unsupported extensions. This milestone accepts ordinary `.py` files only;
`setup.py` and `conftest.py` are also blocked. No blocked-path override is provided.

The parser accepts existing-file `--- a/path` / `+++ b/path` headers, optional
matching `diff --git` and ordinary index metadata, and counted unified hunks.
It verifies old lines against the supplied snapshot, hunk counts, old/new
positions, non-overlap, meaningful changes, and size limits. Creation, deletion,
renames, mode changes, binary/control content, duplicate sections, timestamps,
no-newline markers, and other unsupported syntax fail closed. CRLF source is
normalized for comparison; diffs must use LF. This is structural and source-match
validation, not proof of Python syntax, repair correctness, or safe behavior.

The orchestrator uses the existing repository-context reader to check live paths
and snapshot equality before and after generation. As with context collection,
the checkout must remain stable; this is not race-proof against concurrent host
writers. Any future application must revalidate against its isolated checkout.
Redacted source is ineligible for patching. Known secrets and common sensitive
assignments in candidate diffs cause rejection; this is not a universal secret
detector. Model-selected test references must name supplied test files and are
never executed here. Test edits are permitted by policy, so later verification
must guard against tests being weakened.

Only the validated diagnosis, bounded context/evidence already gathered for it,
a 1-based line index of those same files, and limits are sent to the model. Raw
full logs, host commands, and the whole repository are not sent. No tools are
exposed. One structured request per stage, 4,096 output tokens per request, no
automatic retries or repair loops.

The proposal request differs from diagnosis in two provider-specific ways. Nebius
strict `json_schema` decoding drops newline escapes from string values, which
makes every unified diff collapse onto one line, so proposals use `json_object`
mode with the `PatchProposal` schema embedded in the system prompt. Nemotron's
reasoning also exhausts the 4,096-token budget on diffs without producing an
answer, so thinking is disabled for this request (`enable_thinking: false`). The
response is still parsed with the strict `PatchProposal` model and then
`validate_patch()`; nothing about local validation is relaxed. Diagnosis keeps
strict schema decoding with reasoning enabled.

Offline tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
