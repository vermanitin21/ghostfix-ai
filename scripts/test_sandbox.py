"""Verify the known broken fixture without executing its code on the host."""

import json
from pathlib import Path

from app.core.config import get_settings
from app.sandbox.runner import DockerPytestRunner


def main() -> int:
    try:
        settings = get_settings()
        runner = DockerPytestRunner(settings.sandbox_image, settings.sandbox_timeout_seconds)
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "broken_math"
        result = runner.run(fixture)
    except (ValueError, OSError):
        print(json.dumps({"verified": False, "error": "Invalid sandbox settings or fixture path"}))
        return 1

    verified = (
        result.exit_code == 1
        and not result.timed_out
        and result.infrastructure_error is None
        and result.failed_tests == ["test_calculator.py::test_adds_two_numbers"]
    )
    summary = result.model_dump(exclude={"stdout", "stderr"})
    summary["verified"] = verified
    summary["status"] = "expected_fixture_failure" if verified else "unexpected_sandbox_result"
    if not verified:
        summary["stderr"] = result.stderr
        summary["stdout"] = result.stdout
    print(json.dumps(summary, indent=2))
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
