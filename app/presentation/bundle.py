"""Replay evidence bundle: a sanitized, judge-safe subset of one recorded repair run.

Read-only with respect to repair evidence. The bundle embeds the recorded, schema-valid
ProofOfFix unchanged plus per-attempt provenance selected from the recorded RepairRun.
It never contains credentials, host paths, prompts or raw model payloads.
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.reporting.models import ProofOfFix

SOURCE_FILES = ("proof-of-fix.json", "repair-run.json", "demo-summary.json", "model-audit.json")
_UNSAFE = [
    re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:[^\s\\/\"'<>]+[\\/])+"),          # Windows directories
    re.compile(r"\\\\[A-Za-z0-9_.-]+\\"),                                     # UNC shares
    re.compile(r"(?<![\w.\[-])/(?:tmp|home|Users|var|workspace|root|mnt)/"),  # POSIX host/container dirs
    re.compile(r"ghostfix-verify-"),                                          # verification workspaces
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|authorization)\s*[:=]"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9_.-]{12,}|NEBIUS_API_KEY"),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleProvenance(Strict):
    bundle_schema: Literal["ghostfix-replay-bundle/v1"] = "ghostfix-replay-bundle/v1"
    run_id: str
    derived_from: str
    source_sha256: dict[str, str]
    builder: str
    note: str


class AttemptProvenance(Strict):
    attempt_number: int = Field(ge=1, le=2)
    proposal_model: str
    demo_injected: bool  # decided by the builder script; app/ never knows the injector label
    correction_evidence_supplied: bool
    protocol_retries: int = Field(ge=0)
    recovery_transformations: list[str]
    applied_diff_against_original: str | None


class CorrectionRecord(Strict):
    occurred: bool
    model: str | None
    request_sha256: str | None
    evidence_fields: list[str]
    previous_verification_status: str | None
    targeted_exit_code: int | None
    failed_tests: list[str]
    request_contains_attempt_1_evidence: bool


class IssueRecord(Strict):
    fixture: str
    file: str
    buggy_source: str


class ReplayBundle(Strict):
    provenance: BundleProvenance
    issue: IssueRecord
    proof_of_fix: ProofOfFix
    attempts: list[AttemptProvenance]
    correction: CorrectionRecord
    candidate_2_from_original_source: bool


def unsafe_fragments(text: str, secrets: tuple[str, ...] = ()) -> list[str]:
    """Return pattern names that must never reach the browser."""
    found = [pattern.pattern[:24] for pattern in _UNSAFE if pattern.search(text)]
    if any(secret and secret in text for secret in secrets):
        found.append("configured-secret")
    return found


def _text(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is missing or was withheld by the fidelity policy")
    return value


def build_bundle(source: dict[str, bytes], *, derived_from: str, demo_label: str,
                 secrets: tuple[str, ...] = ()) -> ReplayBundle:
    """Select judge-safe fields from recorded artifact bytes; fail closed on any inconsistency."""
    missing = [name for name in SOURCE_FILES if name not in source]
    if missing:
        raise ValueError(f"Missing canonical evidence: {', '.join(missing)}")
    proof = ProofOfFix.model_validate_json(source["proof-of-fix.json"])
    run = json.loads(source["repair-run.json"])
    summary = json.loads(source["demo-summary.json"])
    audit = json.loads(source["model-audit.json"])
    run_id = proof.provenance.run_id
    if not (run_id == run.get("run_id") == summary.get("run_id")):
        raise ValueError("Run IDs disagree across evidence files")
    if len(run.get("attempts", [])) != proof.provenance.attempt_count or summary.get("proof_status") != proof.status:
        raise ValueError("Attempt count or proof status disagree across evidence files")
    attempts = []
    for item in run["attempts"]:
        proposal = item["proposal_attempt"]
        verification = item.get("verification") or {}
        diff = verification.get("diff_after_apply")
        attempts.append(AttemptProvenance(
            attempt_number=item["attempt_number"], proposal_model=_text(proposal.get("model"), "proposal model"),
            demo_injected=proposal.get("model") == demo_label,
            correction_evidence_supplied=item.get("correction_evidence") is not None,
            protocol_retries=proposal.get("protocol_retry_count", 0),
            recovery_transformations=[t["category"] for e in proposal.get("protocol_events", [])
                                      for t in e.get("transformations", [])],
            applied_diff_against_original=_text(diff, "applied diff") if diff is not None else None))
    if [a.attempt_number for a in attempts] != [t.attempt_number for t in proof.repair_trajectory]:
        raise ValueError("Attempt numbering disagrees with Proof of Fix")
    if any(a.demo_injected for a in attempts) and demo_label not in proof.provenance.runtime_models:
        raise ValueError("Demo provenance disagrees with Proof of Fix")
    correction = summary.get("correction") or {}
    calls = [e for e in audit if e.get("stage") == "correction"]
    if correction.get("occurred"):
        if (len(calls) != 1 or calls[0].get("request_user_content_sha256") != correction.get("request_sha256")
                or calls[0].get("returned_model") != correction.get("model")):
            raise ValueError("Correction record disagrees with the model audit")
    elif calls:
        raise ValueError("Model audit shows a correction call the summary does not")
    reproduction = summary.get("reproduction") or {}
    changed = proof.final_patch.changed_files if proof.final_patch else []
    bundle = ReplayBundle(
        provenance=BundleProvenance(
            run_id=run_id, derived_from=derived_from,
            source_sha256={name: hashlib.sha256(source[name]).hexdigest() for name in SOURCE_FILES},
            builder="scripts/build_replay_bundle.py",
            note="Recorded evidence from the canonical self-correction demo run; candidate 1 was demo-injected. "
                 "Replaying it makes no model, network or Docker call."),
        issue=IssueRecord(fixture="broken_math", file=changed[0] if changed else "calculator.py",
                          buggy_source=_text(reproduction.get("buggy_source"), "buggy source")),
        proof_of_fix=proof, attempts=attempts,
        correction=CorrectionRecord(
            occurred=bool(correction.get("occurred")), model=correction.get("model"),
            request_sha256=correction.get("request_sha256"), evidence_fields=list(correction.get("evidence_fields", [])),
            previous_verification_status=correction.get("previous_verification_status"),
            targeted_exit_code=correction.get("targeted_exit_code"), failed_tests=list(correction.get("failed_tests", [])),
            request_contains_attempt_1_evidence=bool(correction.get("request_contains_attempt_1_evidence"))),
        candidate_2_from_original_source=bool(summary.get("candidate_2_from_original_source")))
    problems = unsafe_fragments(bundle.model_dump_json(), secrets)
    if problems:
        raise ValueError(f"Bundle contains unsafe content: {problems}")
    return bundle


def load_bundle(path: Path, secrets: tuple[str, ...] = ()) -> ReplayBundle:
    """Validate a stored bundle; malformed or unsafe evidence raises instead of rendering."""
    text = path.read_text(encoding="utf-8")
    problems = unsafe_fragments(text, secrets)
    if problems:
        raise ValueError("Replay evidence contains unsafe content")
    return ReplayBundle.model_validate_json(text)


def local_canonical_status(bundle: ReplayBundle, folder: Path) -> Literal["match", "mismatch", "unavailable"]:
    """Hash-only comparison with local canonical artifacts; never loads them into the view."""
    try:
        actual = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    except OSError:
        return "unavailable"
    return "match" if actual == bundle.provenance.source_sha256 else "mismatch"
