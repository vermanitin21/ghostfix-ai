"""Run pytest in an isolated Docker container."""

import subprocess
import re
from pathlib import Path
from time import monotonic
from uuid import uuid4

from app.sandbox.models import SandboxResult


def _text(value: str | bytes | None) -> str:
    # TimeoutExpired may contain bytes even with text=True.
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


class DockerPytestRunner:
    def __init__(self, image: str, timeout_seconds: int = 120) -> None:
        if not image.strip() or image.startswith("-"):
            raise ValueError("A valid sandbox image is required")
        if timeout_seconds <= 0:
            raise ValueError("Sandbox timeout must be positive")
        self._image = image
        self._timeout_seconds = timeout_seconds

    def run(self, project_dir: Path) -> SandboxResult:
        project_dir = project_dir.resolve(strict=True)
        if not project_dir.is_dir():
            raise ValueError(f"Sandbox target is not a directory: {project_dir}")

        if any(char in str(project_dir) for char in ',"\n\r'):
            raise ValueError("Sandbox path contains unsupported Docker mount characters")
        name = f"ghostfix-pytest-{uuid4().hex}"
        command = [
            "docker", "run", "--rm", "--pull=never", f"--name={name}",
            "--network=none", "--user=10001:10001", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=128",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--mount", f"type=bind,source={project_dir},target=/workspace,readonly,bind-recursive=disabled",
            "--workdir=/workspace", "--entrypoint=python", self._image,
            "-m", "pytest", "-q", "-r", "f", "--color=no", "--tb=short",
            "-p", "no:cacheprovider", "--basetemp=/tmp/pytest", ".",
        ]
        started = monotonic()
        timed_out = False
        infrastructure_error = None
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
            )
            exit_code, stdout, stderr = process.returncode, process.stdout, process.stderr
            if exit_code not in (0, 1):
                infrastructure_error = f"Docker/pytest execution did not finish normally (exit {exit_code})"
        except subprocess.TimeoutExpired as error:
            exit_code, stdout, stderr = 124, _text(error.stdout), _text(error.stderr)
            timed_out = True
            infrastructure_error = "Sandbox execution timed out"
            # Killing the attached CLI does not stop the daemon's container.
            try:
                cleanup = subprocess.run(
                    ["docker", "rm", "--force", name], capture_output=True,
                    check=False, text=True, encoding="utf-8", errors="replace", timeout=10,
                )
                if cleanup.returncode != 0:
                    infrastructure_error += f"; cleanup could not be confirmed for {name}"
            except (OSError, subprocess.TimeoutExpired):
                infrastructure_error += f"; cleanup failed for {name}"
        except OSError:
            exit_code, stdout, stderr = 125, "", ""
            infrastructure_error = "Could not launch Docker. Check Docker installation and access."

        # Best-effort node IDs from pytest's short summary, not trusted proof of a fix.
        failed_tests = list(dict.fromkeys(re.findall(
            r"^FAILED ([^\r\n]+?)(?: - .*|\r?)$", stdout, flags=re.MULTILINE,
        ))) if exit_code == 1 else []
        return SandboxResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            duration_ms=round((monotonic() - started) * 1000), command=command,
            timed_out=timed_out, failed_tests=failed_tests,
            infrastructure_error=infrastructure_error,
        )
