# Structured root-cause diagnosis

From the repository root, with the existing sandbox image and Linux Docker engine:

```powershell
.\.venv\Scripts\python.exe -m scripts.test_diagnosis
```

The local `.env` must configure `NEBIUS_API_KEY` and
`NEBIUS_MODEL=nvidia/Nemotron-3_5-Lightning`. The script reads settings without
printing them, reproduces the failure in Docker, and sends a limited diagnosis
request to Nebius. It exits 0 only after schema validation and identification of
`calculator.py`; setup, sandbox, API, schema, and verification failures exit 1.

## Interfaces and evidence

`GhostFixOrchestrator.analyze(repository, paths)` coordinates
`reproduce_failure()`, `gather_context()`, and `diagnose_failure()`. The existing
`reproduce()` interface is retained. `StructuredLLMClient` separates the reasoning
request from deterministic Docker and file-reading operations.

`Diagnosis` contains failure summary, suspected files and symbols, root-cause
hypothesis, supporting evidence, confidence in [0, 1], and recommended next action.
Extra fields and invalid values are rejected. Confidence is a model estimate,
not verified repair correctness. `DiagnosisInput` records exactly the bounded
context sent to the model. `DiagnosisAttempt` retains an ID, UTC timestamp, full
original `SandboxResult`, context, model name when available, diagnosis, and status.
Failures after reproduction retain evidence and a safe error classification.
These records are in memory and JSON-serializable; no database or automatic disk
persistence is added. Raw sandbox evidence may contain sensitive repository text;
only the sanitized context is sent to the model and only the diagnosis summary
is printed by the verification script.

## Context and security

The real check explicitly selects `calculator.py` and `test_calculator.py` relative
to `fixtures/broken_math`. No automatic repository crawl or model-chosen file read
is performed. Limits are four files, 4 KiB per file, 8 KiB total, ten failed-test
IDs (300 characters each), and approximately 2,000 characters per log stream.
The Docker command and host paths are not sent to Nemotron.

`RepositoryContext` accepts an approved root and reads UTF-8 regular text files
only, with a narrow extension allowlist. It rejects traversal, absolute paths,
Windows streams, hidden/environment/secret paths, links, reparse points,
hardlinked files, binary content, and oversized files. Known credentials and
common secret assignments/private-key blocks are redacted from outgoing context.
This is defense in depth, not a universal secret detector: explicitly select
reviewed files, and keep the approved checkout stable during collection. This
helper is not a race-proof filesystem sandbox against concurrent host writers.

Repository text and logs are explicitly marked untrusted in the system prompt.
No command execution or file modification tools are exposed to the model.
Recommended actions remain inert text for human review. Model-suspected paths must
be among the supplied files. Repository code executes only through the existing
restricted Docker runner; no generated code or patches are produced or applied.

The client requests Nebius `json_schema` output and validates the returned JSON
with Pydantic; there is no markdown extraction or free-form fallback. Refusals,
truncated responses, and invalid JSON fail closed. Each request has a 60-second
SDK timeout, a 65-second orchestration deadline, no automatic retries, and a
4,096-token output ceiling (including reasoning). The initial 1,200-token ceiling
was insufficient in live testing. Only one model request is made per attempt.

Unit tests are offline and mock Docker/model execution:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
