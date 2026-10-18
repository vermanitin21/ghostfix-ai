"""Sandbox execution result models."""

from pydantic import BaseModel, ConfigDict, Field


class SandboxResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)
    command: list[str]
    timed_out: bool = False
    failed_tests: list[str] = Field(default_factory=list)
    infrastructure_error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.infrastructure_error is None and self.exit_code == 0
