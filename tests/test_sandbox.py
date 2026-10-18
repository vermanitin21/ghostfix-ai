import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.sandbox.models import SandboxResult
from app.sandbox.runner import DockerPytestRunner
from scripts import test_sandbox


FAILURE = "FAILED test_calculator.py::test_adds_two_numbers - assert -1 == 5\n"


def test_expected_failure_and_security_controls(monkeypatch, tmp_path):
    run = Mock(return_value=subprocess.CompletedProcess([], 1, FAILURE, ""))
    monkeypatch.setattr(subprocess, "run", run)
    result = DockerPytestRunner("test-image").run(tmp_path)
    assert result.exit_code == 1
    assert result.failed_tests == ["test_calculator.py::test_adds_two_numbers"]
    assert not result.passed
    assert result.infrastructure_error is None
    assert result.duration_ms >= 0
    assert result.stdout == FAILURE
    command = run.call_args.args[0]
    assert result.command == command
    for flag in (
        "--network=none", "--user=10001:10001", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--cpus=1", "--memory=512m",
        "--memory-swap=512m", "--pids-limit=128", "--pull=never",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
    ):
        assert flag in command
    assert "--privileged" not in command
    assert f"type=bind,source={tmp_path.resolve()},target=/workspace,readonly,bind-recursive=disabled" in command
    assert run.call_args.kwargs["timeout"] == 120
    assert not run.call_args.kwargs.get("shell", False)
    assert command[-1] == "."


@pytest.mark.parametrize("cleanup_code", [0, 1])
def test_timeout_cleans_container_and_decodes_output(monkeypatch, tmp_path, cleanup_code):
    run = Mock(side_effect=[
        subprocess.TimeoutExpired("docker", 1, output=b"partial\xff", stderr=b"error"),
        subprocess.CompletedProcess([], cleanup_code, "", ""),
    ])
    monkeypatch.setattr(subprocess, "run", run)
    result = DockerPytestRunner("test-image", 1).run(tmp_path)
    assert result.timed_out and result.exit_code == 124
    assert result.stdout == "partial\ufffd"
    assert result.stderr == "error"
    name = next(arg.split("=", 1)[1] for arg in result.command if arg.startswith("--name="))
    assert run.call_args.args[0] == ["docker", "rm", "--force", name]
    assert ("cleanup" in result.infrastructure_error) == (cleanup_code != 0)


@pytest.mark.parametrize("exit_code", [0, 2, 3, 4, 5, 125, 126, 127, 137])
def test_exit_classification(monkeypatch, tmp_path, exit_code):
    monkeypatch.setattr(subprocess, "run", Mock(
        return_value=subprocess.CompletedProcess([], exit_code, "", "daemon error")
    ))
    result = DockerPytestRunner("test-image").run(tmp_path)
    assert result.passed == (exit_code == 0)
    assert (result.infrastructure_error is None) == (exit_code == 0)


def test_missing_docker_is_structured(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=FileNotFoundError))
    result = DockerPytestRunner("test-image").run(tmp_path)
    assert result.exit_code == 125
    assert result.infrastructure_error


def test_rejects_mount_injection(monkeypatch, tmp_path):
    directory = tmp_path / "fixture,readonly=false"
    directory.mkdir()
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="mount characters"):
        DockerPytestRunner("test-image").run(directory)
    run.assert_not_called()


@pytest.mark.parametrize("exit_code,failed_tests,expected", [
    (1, ["test_calculator.py::test_adds_two_numbers"], 0),
    (0, [], 1), (125, [], 1), (1, [], 1),
])
def test_verification_exit_status(monkeypatch, capsys, exit_code, failed_tests, expected):
    result = SandboxResult(exit_code=exit_code, stdout="", stderr="", duration_ms=5,
                           command=["docker"], failed_tests=failed_tests)
    run = Mock(return_value=result)
    monkeypatch.setattr(DockerPytestRunner, "run", run)
    monkeypatch.setattr(test_sandbox, "get_settings", lambda: Settings(_env_file=None))
    assert test_sandbox.main() == expected
    assert run.call_args.args[0] == Path(test_sandbox.__file__).resolve().parents[1] / "fixtures" / "broken_math"
    assert json.loads(capsys.readouterr().out)["verified"] == (expected == 0)
