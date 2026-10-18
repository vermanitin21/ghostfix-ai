"""Apply only to a disposable fixture copy and verify only through Docker."""
import difflib
import tempfile
from pathlib import Path

from app.agent.patches import patched_contents
from app.agent.state import PatchProposalAttempt, PatchVerification, VerificationTests
from app.sandbox.runner import DockerPytestRunner
from app.sandbox.test_ids import validate_test_ids
from app.tools.files import RepositoryContext, redact


def fixture_snapshot(repository: RepositoryContext) -> dict[str, bytes]:
    """Bounded fixture profile: copy all Python files, reject other active content."""
    paths = []
    for path in repository.root.rglob("*"):
        relative = path.relative_to(repository.root)
        if any(part in {"__pycache__", ".pytest_cache", ".git"} for part in relative.parts):
            continue
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Links are forbidden in verification copies")
        if path.is_dir():
            continue
        if path.suffix != ".py":
            raise ValueError("Verification currently supports Python-only fixtures")
        paths.append(relative.as_posix())
        if len(paths) > repository.MAX_FILES:
            raise ValueError("Verification fixture exceeds copy budget")
    files = repository.read_files(paths)
    snapshot = {}
    for file in files:
        data = (repository.root / file.path).read_bytes()
        if data.decode("utf-8") != file.content:
            raise ValueError("Redacted or changed files cannot be copied")
        snapshot[file.path] = data
    return snapshot


class PatchVerifier:
    def __init__(self, sandbox: DockerPytestRunner) -> None:
        self.sandbox = sandbox

    def verify(self, attempt: PatchProposalAttempt, repository: RepositoryContext) -> PatchVerification:
        result = PatchVerification(proposal_attempt=attempt, risk_notes=[
            "Verification covers only the supplied Python fixture and its existing tests.",
        ])
        workspace = None
        before = None
        try:
            if not attempt.validation_result or not attempt.validation_result.valid or not attempt.proposal:
                raise ValueError("A validated proposal is required")
            context = attempt.diagnosis_attempt.context
            if context is None:
                raise ValueError("Missing source context")
            before = fixture_snapshot(repository)
            if any(before.get(file.path) != file.content.encode("utf-8") for file in context.files):
                raise ValueError("Source changed since diagnosis")
            proposal = attempt.proposal
            # Do not accept passing tests obtained by weakening the tests themselves.
            if any(Path(name).name.startswith("test_") for name in proposal.target_files):
                raise ValueError("Test modifications are not allowed during verification")
            expected = patched_contents(proposal, context, repository.root, attempt.validation_result.limits)
            validate_test_ids(repository.root, proposal.tests_to_run)
            with tempfile.TemporaryDirectory(prefix="ghostfix-verify-") as directory:
                workspace = Path(directory).resolve()
                if workspace == repository.root or workspace.is_relative_to(repository.root):
                    raise ValueError("Temporary workspace must be outside approved repository")
                for name, data in before.items():
                    target = workspace / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                for name, content in expected.items():
                    target = (workspace / name).resolve(strict=True)
                    if not target.is_relative_to(workspace) or target.is_symlink():
                        raise ValueError("Application path escaped temporary workspace")
                    original = before[name]
                    newline = "\r\n" if b"\r\n" in original else "\n"
                    target.write_bytes(content.replace("\n", newline).encode("utf-8"))
                actual = {path.relative_to(workspace).as_posix(): path.read_bytes()
                          for path in workspace.rglob("*") if path.is_file()}
                expected_bytes = dict(before)
                for name, content in expected.items():
                    newline = "\r\n" if b"\r\n" in before[name] else "\n"
                    expected_bytes[name] = content.replace("\n", newline).encode("utf-8")
                if actual != expected_bytes:
                    raise ValueError("Applied workspace differs from validated proposal")
                result.changed_files = sorted(name for name in actual if actual[name] != before[name])
                if set(result.changed_files) != set(proposal.target_files):
                    raise ValueError("Unexpected changed-file set")
                result.diff_after_apply = "".join("".join(difflib.unified_diff(
                    before[name].decode().replace("\r\n", "\n").splitlines(keepends=True),
                    actual[name].decode().replace("\r\n", "\n").splitlines(keepends=True),
                    fromfile=f"a/{name}", tofile=f"b/{name}",
                )) for name in result.changed_files)
                # Canonical diff may use different context widths; require identical effects.
                canonical = proposal.model_copy(update={"unified_diff": result.diff_after_apply})
                if patched_contents(canonical, context, repository.root, attempt.validation_result.limits) != expected:
                    raise ValueError("Resulting diff does not match proposal effects")
                result.applied = True
                for stage, selectors in (("targeted", proposal.tests_to_run), ("regression", None)):
                    evidence = self.sandbox.run(workspace, selectors)
                    tests = VerificationTests(
                        command=evidence.command, exit_code=evidence.exit_code, passed=evidence.passed,
                        failed_tests=evidence.failed_tests, duration_ms=evidence.duration_ms, evidence=evidence,
                        stdout_excerpt=redact(evidence.stdout, repository.secrets)[:2000],
                        stderr_excerpt=redact(evidence.stderr, repository.secrets)[:2000],
                    )
                    setattr(result, f"{stage}_tests", tests)
                    if not evidence.passed:
                        result.status = ("timeout" if evidence.timed_out else "infrastructure_failure"
                                         if evidence.infrastructure_error else "targeted_test_failure"
                                         if stage == "targeted" else "regression_failure")
                        result.failure_reason = f"{stage} verification failed"
                        break
                else:
                    result.verified, result.status = True, "verified"
        except Exception as error:
            result.verified = False
            result.status = "infrastructure_failure" if result.applied else "application_failure"
            result.failure_reason = f"Verification aborted ({type(error).__name__})"
        finally:
            result.workspace_cleaned = workspace is None or not workspace.exists()
            try:
                result.original_unchanged = before is not None and fixture_snapshot(repository) == before
            except (OSError, ValueError):
                result.original_unchanged = False
            if not result.workspace_cleaned or (before is not None and not result.original_unchanged):
                result.verified, result.status = False, "infrastructure_failure"
                result.failure_reason = "Cleanup or original-source integrity check failed"
        return result
