"""Render only a builder-produced proof; model text is displayed as inert evidence."""
import html
import re

from app.reporting.models import ProofOfFix, ProofTest


def render_json(proof: ProofOfFix) -> str:
    return proof.model_dump_json(indent=2)


def _block(text: str, language: str = "text") -> str:
    # A fenced code block renders HTML inert; content cannot close its fence.
    fence = "`" * max(3, max((len(part) for part in re.findall(r"`+", text)), default=0) + 1)
    body = text.rstrip("\n")
    return f"{fence}{language}\n{body}\n{fence}"


def _status(test: ProofTest | None) -> str:
    return "NOT AVAILABLE" if test is None else "PASS" if test.passed else "FAIL"


def render_markdown(proof: ProofOfFix) -> str:
    sections = ["# GhostFix — Proof of Fix", "## Status", proof.status,
                "## Reproduction", "Original failure reproduced." if proof.reproduction.reproduced else "Original failure not confirmed.",
                _block("\n".join(proof.reproduction.failed_tests) or "Failed test IDs not available."),
                "## Root Cause (model hypothesis)",
                _block(proof.diagnosis.root_cause_hypothesis if proof.diagnosis else "Not available."),
                "## Repair Trajectory"]
    for attempt in proof.repair_trajectory:
        sections += [f"### Attempt {attempt.attempt_number} — {html.escape(attempt.verification_status.upper())}",
            f"Proposal validation: {'PASS' if attempt.proposal_valid else 'FAIL / NOT AVAILABLE'}",
            _block(attempt.summary or "No candidate summary."),
            f"Targeted verification: {_status(attempt.targeted)}  \nRegression verification: {_status(attempt.regression)}"]
        if attempt.failure_reason:
            sections.append(_block(attempt.failure_reason))
        if attempt.candidate_diff:
            sections += ["Candidate patch (proposal evidence)", _block(attempt.candidate_diff, "diff")]
        for test in (attempt.targeted, attempt.regression):
            if test and not test.passed:
                sections.append(_block("\n".join(test.failed_tests) + "\n" + test.stdout_excerpt + "\n" + test.stderr_excerpt))
    sections += ["## Final Verified Patch", _block(proof.final_patch.unified_diff, "diff") if proof.final_patch else "No verified final patch available.",
                 "## Verification", f"Targeted: {_status(proof.verification.targeted)}  \nRegression: {_status(proof.verification.regression)}"]
    for label, test in (("Targeted pytest argv", proof.verification.targeted), ("Regression pytest argv", proof.verification.regression)):
        if test:
            sections += [label, _block(" ".join(test.pytest_argv) if test.pytest_argv else "Not available.")]
    def integrity(value):
        return "NOT AVAILABLE" if value is None else "PASS" if value else "FAIL"
    sections += ["## Integrity", f"Original repository unchanged: {integrity(proof.integrity.original_repository_unchanged)}  \nWorkspace cleaned: {integrity(proof.integrity.workspace_cleaned)}",
                 "## Risks and Limitations", _block("\n".join(proof.risks)), "## Evidence",
                 f"Attempts: {proof.provenance.attempt_count}", _block("\n".join(proof.provenance.runtime_models) or "Model not available.")]
    if proof.verification.evidence_issues:
        sections += ["Evidence issues", _block("\n".join(proof.verification.evidence_issues))]
    return "\n\n".join(sections) + "\n"
