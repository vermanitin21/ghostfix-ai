# Docker fixture verification

Run from the repository root with Docker Desktop running Linux containers:

```powershell
docker build -f Dockerfile.sandbox -t ghostfix-pytest-sandbox:latest .
.\.venv\Scripts\python.exe -m scripts.test_sandbox
```

The script reads `SANDBOX_IMAGE` and `SANDBOX_TIMEOUT_SECONDS` from existing settings.
The image tag above matches the default; use your configured tag if overridden.
Image building requires network access to download Python and pytest; test execution
has networking disabled and never pulls an image automatically.

Expected outcome: JSON with `verified: true`, `exit_code: 1`, and
`failed_tests: ["test_calculator.py::test_adds_two_numbers"]`. The script exits 0
for this expected bug reproduction. Unexpected results, missing Docker/image,
collection errors, and timeouts cause a non-zero script exit.

The runner returns a Pydantic `SandboxResult` containing exit status, full stdout
and stderr, elapsed milliseconds (including cleanup), the Docker argument list,
timeout status, failed test IDs, and any infrastructure error. Failed test IDs
are best-effort extraction from pytest's short summary. They are suitable for
this deterministic fixture, not authenticated evidence from hostile test code.

Runtime controls:

- No external network (`--network=none`).
- Non-root UID/GID 10001, all Linux capabilities dropped, no new privileges.
- Read-only root and fixture bind mount; nested host mounts excluded.
- A 64 MiB temporary `/tmp` with noexec/nosuid/nodev; pytest cache disabled.
- One CPU, 512 MiB memory with no extra swap, 128 processes.
- Default 120-second execution timeout; forced removal of the named container
  on timeout, with a separate 10-second cleanup timeout. Cleanup failures are reported.
- No privileged mode, Docker socket mount, or host execution of repository code.
- Build context excludes repository files and secrets via `.dockerignore`.

Requires a local Linux Docker engine supporting `bind-recursive=disabled` and
resource limits. Containers share the Docker VM/host kernel; this is not a VM
security boundary. A daemon outage can prevent cleanup; the result includes the
container name when cleanup cannot be confirmed. Output is captured in memory,
so this foundation is intended for bounded fixtures, not arbitrary output floods.

Backend unit tests mock Docker and do not execute fixture code:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
