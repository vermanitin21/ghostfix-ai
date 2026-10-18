"""Presentation adapter: ReplayBundle -> judge-facing view model.

Outcomes (PASS, FAIL, VERIFIED) come only from structured ProofOfFix/RepairRun fields.
Text parsing is used solely for display details (test counts, observed vs expected
values) and never decides success; unparseable text yields "unavailable", not a guess.
"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.presentation.bundle import ReplayBundle
from app.reporting.models import ProofTest

Tone = Literal["pass", "fail", "model", "evidence", "neutral"]
# Locked official results (same frozen 7-case corpus, one official run each). Never recomputed here.
BENCHMARKS = (("v1", 1, 7), ("v2", 3, 7), ("v3", 4, 7))
_SUMMARY = re.compile(r"^(?:=+ )?((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)(?:, )?)+)"
                      r" in [\d.]+s(?: =+)?$")


class View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiffLine(View):
    kind: Literal["file", "hunk", "add", "del", "ctx"]
    text: str


class TestView(View):
    result: Literal["PASS", "FAIL"]
    exit_code: int
    counts_text: str | None
    failed_tests: list[str]
    command: str | None
    key_lines: list[str]


class Stage(View):
    key: str
    step: int
    title: str
    status: str
    tone: Tone


class CandidateView(View):
    attempt: int
    provenance: Literal["DEMO-INJECTED CANDIDATE", "MODEL-GENERATED CANDIDATE"]
    origin: str
    diff: list[DiffLine]
    targeted: TestView | None
    regression: TestView | None
    outcome: Literal["PASSED", "FAILED", "NOT VERIFIED"]
    tone: Tone
    applied_to_original: bool | None
    protocol_note: str


class EvidenceView(View):
    target: str
    exit_code: int
    observed: str | None
    expected: str | None
    expression: str | None
    status: Literal["FAILED"]
    key_lines: list[str]
    supplied_to_correction: bool


class CorrectionView(View):
    occurred: bool
    model: str | None
    inputs: list[str]
    request_sha256: str | None


class CheckItem(View):
    label: str
    ok: bool


class ProofView(View):
    status: str
    display_status: str
    verified: bool
    run_id: str
    generated_at: str
    checklist: list[CheckItem]
    evidence_issues: list[str]


class BenchmarkView(View):
    version: str
    verified: int
    total: int
    percent: str


class ReplayView(View):
    mode: Literal["RECORDED"] = "RECORDED"
    mode_label: str
    run_id: str
    local_canonical: Literal["match", "mismatch", "unavailable"]
    issue_file: str
    target_test: str | None
    reproduced: bool
    reproduction_exit_code: int
    reproduction_observed: str | None
    buggy_source: str
    stages: list[Stage]
    diagnosis_hypothesis: str | None
    diagnosis_model: str | None
    candidates: list[CandidateView]
    evidence: EvidenceView | None
    correction: CorrectionView
    proof: ProofView
    benchmarks: list[BenchmarkView]


def summary_counts(test: ProofTest) -> str | None:
    """Display-only count from pytest's final summary line; None if absent or contradictory."""
    lines = [line.strip() for line in test.stdout_excerpt.splitlines() if line.strip()]
    match = _SUMMARY.match(lines[-1]) if lines else None
    if not match:
        return None
    counts = {kind.rstrip("s") if kind in ("errors", "warnings") else kind: int(number)
              for number, kind in re.findall(r"(\d+) (\w+)", match.group(1))}
    passed, failed, errors = counts.get("passed", 0), counts.get("failed", 0), counts.get("error", 0)
    total = passed + failed + errors
    if not total or test.passed != (failed == 0 and errors == 0):
        return None  # never display counts that contradict the structured result
    return f"{passed}/{total} passed"


def _key_lines(test: ProofTest) -> list[str]:
    return [line for line in test.stdout_excerpt.splitlines()
            if re.match(r"\s*(E\s|FAILED |assert )", line)][:6]


def _command(test: ProofTest) -> str | None:
    return " ".join(test.pytest_argv) if test.pytest_argv else None


def _test(test: ProofTest | None) -> TestView | None:
    if test is None:
        return None
    return TestView(result="PASS" if test.passed else "FAIL", exit_code=test.exit_code,
                    counts_text=summary_counts(test), failed_tests=test.failed_tests[:10],
                    command=_command(test), key_lines=[] if test.passed else _key_lines(test))


def observed_expected(text: str) -> tuple[str | None, str | None, str | None]:
    """Display-only reading of pytest's assertion rewrite: observed, expected, expression."""
    assertion = re.search(r"^E\s+assert (.+?) == (.+?)\s*$", text, re.M)
    if not assertion:
        return None, None, None
    observed, expected = assertion.groups()
    where = re.search(r"^E\s+\+\s+where (.+?) = (.+?)\s*$", text, re.M)
    expression = where.group(2) if where and where.group(1) == observed else None
    return observed, expected, expression


def _diff(text: str | None) -> list[DiffLine]:
    lines = []
    for line in (text or "").splitlines():
        kind = ("file" if line.startswith(("--- ", "+++ ")) else "hunk" if line.startswith("@@")
                else "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx")
        lines.append(DiffLine(kind=kind, text=line))
    return lines


def build_view(bundle: ReplayBundle, local_canonical: Literal["match", "mismatch", "unavailable"] = "unavailable") -> ReplayView:
    proof = bundle.proof_of_fix
    verified = bool(proof.verified and proof.status == "VERIFIED" and not proof.verification.evidence_issues)
    attempts = {a.attempt_number: a for a in bundle.attempts}
    if any(a.demo_injected and a.proposal_model not in proof.provenance.runtime_models for a in bundle.attempts):
        raise ValueError("Demo provenance disagrees with Proof of Fix")
    candidates = []
    for shown in proof.repair_trajectory:
        meta = attempts[shown.attempt_number]
        injected = meta.demo_injected
        targeted, regression = _test(shown.targeted), _test(shown.regression)
        passed = bool(shown.targeted and shown.targeted.passed and shown.regression and shown.regression.passed)
        failed = bool((shown.targeted and not shown.targeted.passed) or (shown.regression and not shown.regression.passed))
        outcome = "PASSED" if passed else "FAILED" if failed else "NOT VERIFIED"
        notes = ", ".join(meta.recovery_transformations) or "raw diff valid as written"
        candidates.append(CandidateView(
            attempt=shown.attempt_number,
            provenance="DEMO-INJECTED CANDIDATE" if injected else "MODEL-GENERATED CANDIDATE",
            origin=("Deliberately wrong patch injected by the demo script - not model output" if injected
                    else f"Generated by {meta.proposal_model}" + (" from executed failure evidence"
                                                                  if meta.correction_evidence_supplied else "")),
            diff=_diff(shown.candidate_diff), targeted=targeted, regression=regression, outcome=outcome,
            tone="pass" if outcome == "PASSED" else "fail" if outcome == "FAILED" else "neutral",
            applied_to_original=(bundle.candidate_2_from_original_source if meta.correction_evidence_supplied else None),
            protocol_note="" if injected else f"Protocol: {notes}; retries: {meta.protocol_retries}"))
    first = proof.repair_trajectory[0] if proof.repair_trajectory else None
    second = candidates[1] if len(candidates) > 1 else None
    evidence = None
    if first and first.targeted and not first.targeted.passed:
        observed, expected, expression = observed_expected(first.targeted.stdout_excerpt)
        evidence = EvidenceView(
            target=(first.targeted.failed_tests or ["unknown"])[0], exit_code=first.targeted.exit_code,
            observed=observed, expected=expected, expression=expression, status="FAILED",
            key_lines=_key_lines(first.targeted),
            supplied_to_correction=bool(bundle.correction.occurred and bundle.correction.request_contains_attempt_1_evidence
                                        and second is not None and attempts[2].correction_evidence_supplied))
    labels = {"previous_proposal": "Failed Candidate 1 patch", "verification_status": "Verification status",
              "targeted_tests": "Failed test ID, exit code and bounded pytest output",
              "regression_tests": "Regression result field (empty when not reached)", "changed_files": "Changed files"}
    fields = sorted(bundle.correction.evidence_fields, key=lambda f: list(labels).index(f) if f in labels else len(labels))
    correction = CorrectionView(
        occurred=bundle.correction.occurred, model=bundle.correction.model,
        inputs=(["Original diagnosis and source context"] + [labels.get(f, f) for f in fields]
                if bundle.correction.occurred else []),
        request_sha256=bundle.correction.request_sha256)
    final_targeted, final_regression = proof.verification.targeted, proof.verification.regression
    counts = lambda test: f" - {summary_counts(test)}" if test and summary_counts(test) else ""
    checklist = [
        CheckItem(label="Original failure reproduced", ok=proof.reproduction.reproduced),
        CheckItem(label="Candidate 1 executed and failed", ok=bool(first and first.targeted and not first.targeted.passed)),
        CheckItem(label="Failure evidence captured and supplied", ok=bool(evidence and evidence.supplied_to_correction)),
        CheckItem(label="Candidate 2 generated by the model", ok=bool(second and second.provenance == "MODEL-GENERATED CANDIDATE")),
        CheckItem(label="Targeted tests passed" + counts(final_targeted), ok=bool(final_targeted and final_targeted.passed)),
        CheckItem(label="Regression tests passed" + counts(final_regression), ok=bool(final_regression and final_regression.passed)),
        CheckItem(label="Original source unchanged", ok=proof.integrity.original_repository_unchanged is True),
        CheckItem(label="Temporary workspaces cleaned", ok=proof.integrity.workspace_cleaned is True),
    ]
    # A VERIFIED claim with contradicting flags or evidence issues is never displayed as verified.
    display = ("VERIFIED" if verified else "UNSUPPORTED CLAIM - NOT PROVEN" if proof.status == "VERIFIED"
               else proof.status.replace("_", " "))
    stages = [Stage(key="reproduce", step=1, title="Reproduce",
                    status="FAIL REPRODUCED" if proof.reproduction.reproduced else "NOT REPRODUCED",
                    tone="fail" if proof.reproduction.reproduced else "neutral"),
              Stage(key="diagnose", step=2, title="Diagnose", status="COMPLETE" if proof.diagnosis else "UNAVAILABLE",
                    tone="model" if proof.diagnosis else "neutral")]
    for candidate in candidates:
        if candidate.attempt == 2:
            stages += [Stage(key="evidence", step=len(stages) + 1, title="Evidence",
                             status="CAPTURED" if evidence and evidence.supplied_to_correction else "NOT CAPTURED",
                             tone="evidence" if evidence and evidence.supplied_to_correction else "neutral"),
                       Stage(key="correct", step=len(stages) + 2, title="Self-correct",
                             status="COMPLETE" if correction.occurred else "NOT PERFORMED",
                             tone="model" if correction.occurred else "neutral")]
        stages.append(Stage(key=f"attempt{candidate.attempt}", step=len(stages) + 1, title=f"Attempt {candidate.attempt}",
                            status=candidate.outcome, tone=candidate.tone))
    stages.append(Stage(key="proof", step=len(stages) + 1, title="Proof of Fix", status=display,
                        tone="pass" if verified else "fail"))
    repro_observed, _, repro_expr = observed_expected(proof.reproduction.stdout_excerpt)
    return ReplayView(
        mode_label="RECORDED VERIFIED RUN" if verified else "RECORDED RUN - NOT VERIFIED",
        run_id=proof.provenance.run_id, local_canonical=local_canonical,
        issue_file=f"{bundle.issue.fixture}/{bundle.issue.file}",
        target_test=(proof.reproduction.failed_tests or [None])[0], reproduced=proof.reproduction.reproduced,
        reproduction_exit_code=proof.reproduction.exit_code,
        reproduction_observed=f"{repro_expr} returned {repro_observed}" if repro_expr and repro_observed else None,
        buggy_source=bundle.issue.buggy_source, stages=stages,
        diagnosis_hypothesis=proof.diagnosis.root_cause_hypothesis if proof.diagnosis else None,
        diagnosis_model=proof.diagnosis.model if proof.diagnosis else None,
        candidates=candidates, evidence=evidence, correction=correction,
        proof=ProofView(status=proof.status, display_status=display, verified=verified, run_id=proof.provenance.run_id,
                        generated_at=proof.generated_at.isoformat(), checklist=checklist,
                        evidence_issues=proof.verification.evidence_issues),
        benchmarks=[BenchmarkView(version=v, verified=n, total=t, percent=f"{100 * n / t:.2f}%") for v, n, t in BENCHMARKS])
