# Deterministic Proof of Fix

## Real integration verification

```powershell
.\.venv\Scripts\python.exe -m scripts.test_proof_of_fix
```

This invokes the existing real self-correction demo, which deliberately injects
the valid but wrong first candidate and uses real Nemotron for correction. Its
actual in-memory `RepairRun` is passed directly to the deterministic builder.
The integration writes `artifacts/proof-of-fix.json` and `artifacts/proof-of-fix.md`
(gitignored). It exits 0 only when both the demo trajectory and independent proof
status pass. It checks known credentials and host/workspace paths before writing.
The proof builder itself makes no LLM calls; the demo performs diagnosis/correction.

Reporting consumes an existing `RepairRun`; it does not run repair, tests, shell
commands, repository code, network requests or LLM calls. The repair foundations
and verifier are unchanged.

```python
from app.reporting import build_proof_of_fix, render_json, render_markdown

proof = build_proof_of_fix(run, secrets=(configured_api_key,))
json_artifact = render_json(proof)
markdown_artifact = render_markdown(proof)
```

The builder returns a typed `ProofOfFix` with schema version `1.0`, reproduction,
diagnosis, ordered trajectory, final patch, verification, integrity, risks and
provenance. All text produced by the model is descriptive evidence, not authority.
The root cause is explicitly a hypothesis, and confidence is not used for status.

## Status derivation

VERIFIED requires consistent successful run/final-verification flags, successful
application, passing targeted and regression results that agree with their raw
`SandboxResult`, and passing original-integrity and workspace-cleanup checks.
The final verification must belong to the last candidate/attempt. Ordering,
diagnosis/candidate linkage, prior integrity/cleanup and completion are checked.
The original reproduction must be a normal pytest failure rather than a timeout
or infrastructure error. The final candidate must be valid and its verified diff
must produce the same in-memory effects as its proposal. This reuses pure diff
validation; nothing is applied to disk or executed.

Contradictory or missing evidence yields INCOMPLETE_EVIDENCE and no final verified
patch. A consistently failed verification yields VERIFICATION_FAILED. The schema
also reserves NOT_VERIFIED for non-success without evidence contradictions.
Passing individual test results do not override missing overall evidence.

## Final patch and trajectory

The final patch is the exact validated proposal diff associated with the actual
final verification, not regenerated text. The applied canonical diff is compared
for equivalent effects. Additions/deletions are counted from validated hunks.
If the exact diff cannot be safely disclosed, the report fails closed rather than
displaying a redacted patch as the exact verified candidate. Failed attempts retain
their summaries, bounded diff, failed IDs, failure reasons and test evidence.

## Rendering and privacy

JSON uses Pydantic serialization with stable fields and no ANSI formatting. Unknown
facts remain null: `RepairRun` does not retain the demo's independent hash-check
result or correction-request audit digest, so the report does not manufacture them.
Its `original_repository_unchanged` field reflects verifier evidence across attempts.
Provenance retains reported model identities, including an injected demo identity
when present, rather than presenting injected candidates as model-generated.

The builder redacts supplied known secrets, common sensitive assignments, ANSI/control
characters, and absolute host paths. Logs are bounded to 2,000 characters plus a
truncation marker. It omits raw Docker commands/mounts and exposes a safe pytest
argv tail only when recognizable. Markdown renders model text in code blocks with
fences chosen so embedded Markdown/HTML cannot create fake status sections.
Use builder-produced models with renderers; renderers are not a security boundary
for manually forged `ProofOfFix` objects. Pass secrets explicitly to the builder;
no universal secret detector or evidence authenticity guarantee is claimed.

## Render an existing saved run

The CLI accepts a trusted internal `RepairRun` JSON file (maximum 2 MB), reads the
configured Nebius key only for redaction, and prints to stdout. It does not create
a run or make API calls. A successful rendering exits 0 even if the report's repair
status is non-success; inspect `verified`/`status` to determine repair outcome.

```powershell
.\.venv\Scripts\python.exe -m scripts.render_proof .\repair-run.json --format json
.\.venv\Scripts\python.exe -m scripts.render_proof .\repair-run.json --format markdown
```

The path above is an example; no saved run is assumed or fabricated. Callers can
serialize an actual collected run using `run.model_dump_json()`.

For reproducible snapshots pass an explicit timezone-aware `generated_at` to the
builder. Otherwise only the generation timestamp changes between identical builds.
Reporting does not certify historical evidence against tampering or establish
formal correctness. Coverage remains limited to the executed tests and current
Python fixture adapter.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_proof.py
.\.venv\Scripts\python.exe -m pytest
```
