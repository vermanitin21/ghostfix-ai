# Isolated patch verification

From the repository root, with the existing Docker image and Nebius settings:

```powershell
.\.venv\Scripts\python.exe -m scripts.test_patch_verification
```

This makes one diagnosis request and one proposal request. There are no retries
or second candidates. Only the original fixture is used for reproduction; the
candidate is applied to a `TemporaryDirectory` outside the approved repository.
The original fixture is never written. File hashes are compared before and after
the real script. All repository tests execute in restricted Docker containers.

`PatchVerifier.verify(proposal_attempt, repository)` returns `PatchVerification`,
which embeds the entire prior evidence/diagnosis/proposal chain. Targeted and
regression `VerificationTests` contain bounded excerpts and full `SandboxResult`
evidence. Status distinguishes application failure, targeted failure, regression
failure, infrastructure failure, timeout, and verified success. Records remain
in memory and can be serialized for a future report.

The copy profile intentionally supports small Python-only fixtures (the existing
context limits: four files, 4 KiB each, 8 KiB total). It includes every accepted
Python source/test file, skips `.git`, pytest caches and bytecode caches, and
rejects other active content. It does not silently omit configuration or dependency
files. Larger/configured repositories require a separately reviewed copy policy.
Links, reparse points, hardlinks, secrets, redacted files and unsafe paths are
rejected through the existing context reader. The approved checkout must remain
stable during collection; this is not race-proof against hostile concurrent host
writers. Temporary files are owned by the application, not the model.

Application revalidates the proposal against the current source snapshot and
blocked-path rules. Hunks are processed as data to compute replacement text in
memory. Only the temporary copy is written. The full post-write byte map must
equal the expected map, the changed-file set must match declared targets, and a
canonical regenerated diff must produce the same effects as the proposal.
Canonical hunk context may differ from the original diff's formatting.
Line endings are preserved for ordinary LF/CRLF files. No git hooks, patch shell
commands, imports, or repository programs run on the host.

Test-file changes are rejected at verification time to prevent passing by weakening
the supplied tests. Selectors must be relative `test_*.py` paths, optionally followed
by one or two `::identifier` components. Shell syntax, option strings, absolute paths,
traversal, and parametrized selectors are unsupported and rejected. The runner
receives a list of argv elements with `shell=False`, never a shell string.

Targeted success is required before the full copied-fixture suite (`pytest .`).
Both must pass to mark a repair verified. The Docker runner retains network denial,
non-root identity, read-only mounts, capability restrictions, resource limits and
timeout cleanup. This verifies existing test coverage, not universal correctness.

The temporary-directory context manager cleans up on success and ordinary failure;
cleanup failure makes verification fail. Containers use `--rm`; the existing runner
forces removal after execution timeout and reports cleanup errors. Daemon outages,
process termination, or host crashes can prevent cleanup; there is no guarantee
against those external failures. No automatic recovery loop is added.

Offline tests (no repository code executed on the host):

```powershell
.\.venv\Scripts\python.exe -m pytest
```
