"""Presentation layer: evidence-derived view model, fail-safe loading, security. No model, Docker or network."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.presentation.routes as routes
from app.main import app
from app.presentation.bundle import ReplayBundle, build_bundle, load_bundle, local_canonical_status, unsafe_fragments
from app.presentation.view import ReplayView, build_view, summary_counts
from scripts.run_self_correction_demo import DEMO_MODEL as DEMO_LABEL

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "aa6b79d21b954663b378a40f610d3334"
MODEL = "nvidia/Nemotron-3_5-Lightning"


@pytest.fixture
def bundle() -> ReplayBundle:
    return load_bundle(routes.BUNDLE_PATH)


def with_proof(bundle: ReplayBundle, **changes) -> ReplayBundle:
    return bundle.model_copy(update={"proof_of_fix": bundle.proof_of_fix.model_copy(update=changes)})


def with_attempt_test(bundle: ReplayBundle, attempt: int, stage: str, **changes) -> ReplayBundle:
    trajectory = list(bundle.proof_of_fix.repair_trajectory)
    item = trajectory[attempt - 1]
    trajectory[attempt - 1] = item.model_copy(update={stage: getattr(item, stage).model_copy(update=changes)})
    return with_proof(bundle, repair_trajectory=trajectory)


def test_canonical_view_is_derived_from_evidence(bundle):
    view = build_view(bundle)
    assert view.run_id == RUN_ID and view.mode == "RECORDED" and view.mode_label == "RECORDED VERIFIED RUN"
    assert view.proof.verified and view.proof.display_status == "VERIFIED"
    assert [(s.title, s.status) for s in view.stages] == [
        ("Reproduce", "FAIL REPRODUCED"), ("Diagnose", "COMPLETE"), ("Attempt 1", "FAILED"), ("Evidence", "CAPTURED"),
        ("Self-correct", "COMPLETE"), ("Attempt 2", "PASSED"), ("Proof of Fix", "VERIFIED")]
    first, second = view.candidates
    assert first.provenance == "DEMO-INJECTED CANDIDATE" and "not model output" in first.origin
    assert second.provenance == "MODEL-GENERATED CANDIDATE" and MODEL in second.origin
    assert first.targeted.result == "FAIL" and first.targeted.counts_text == "0/1 passed" and first.outcome == "FAILED"
    assert second.targeted.counts_text == second.regression.counts_text == "4/4 passed" and second.outcome == "PASSED"
    assert [l.text for l in first.diff if l.kind == "add"] == ["+    return left * right"]
    assert [l.text for l in second.diff if l.kind == "add"] == ["+    return left + right"]
    assert (view.evidence.observed, view.evidence.expected, view.evidence.expression) == ("6", "5", "add(2, 3)")
    assert view.evidence.exit_code == 1 and view.evidence.supplied_to_correction
    assert view.diagnosis_model == MODEL and "subtraction" in view.diagnosis_hypothesis
    assert all(item.ok for item in view.proof.checklist) and len(view.proof.checklist) == 8


def test_counts_come_from_evidence_and_never_contradict_structure(bundle):
    test = bundle.proof_of_fix.repair_trajectory[1].targeted
    assert summary_counts(test) == "4/4 passed"
    assert summary_counts(test.model_copy(update={"stdout_excerpt": "....\n3 passed in 0.10s\n"})) == "3/3 passed"
    # Structured failure with text claiming success: result is FAIL and no count is displayed.
    view = build_view(with_attempt_test(bundle, 2, "targeted", passed=False))
    assert view.candidates[1].targeted.result == "FAIL" and view.candidates[1].targeted.counts_text is None
    assert view.candidates[1].outcome == "FAILED"
    # Unparseable text never changes the structured result.
    view = build_view(with_attempt_test(bundle, 2, "targeted", stdout_excerpt="all good, trust me"))
    assert view.candidates[1].targeted.result == "PASS" and view.candidates[1].targeted.counts_text is None


@pytest.mark.parametrize("changes", [
    {"status": "VERIFICATION_FAILED", "verified": False},
    {"status": "NOT_VERIFIED", "verified": False},
    {"status": "INCOMPLETE_EVIDENCE", "verified": False},
    {"verified": False},                         # claim without the flag
])
def test_failed_or_contradictory_proof_never_renders_verified(bundle, changes):
    view = build_view(with_proof(bundle, **changes))
    assert not view.proof.verified and view.proof.display_status != "VERIFIED"
    assert view.mode_label == "RECORDED RUN - NOT VERIFIED" and view.stages[-1].tone == "fail"


def test_evidence_issues_override_a_verified_claim(bundle):
    proof = bundle.proof_of_fix
    issues = proof.verification.model_copy(update={"evidence_issues": ["Attempt 2 integrity or cleanup did not pass."]})
    view = build_view(with_proof(bundle, verification=issues))
    assert not view.proof.verified and view.proof.display_status == "UNSUPPORTED CLAIM - NOT PROVEN"


def test_provenance_labels_follow_recorded_attempts(bundle):
    attempts = [a.model_copy(update={"proposal_model": MODEL, "demo_injected": False}) for a in bundle.attempts]
    view = build_view(bundle.model_copy(update={"attempts": attempts}))
    assert all(c.provenance == "MODEL-GENERATED CANDIDATE" for c in view.candidates)
    # Demo provenance must agree with the Proof of Fix runtime models.
    provenance = bundle.proof_of_fix.provenance.model_copy(update={"runtime_models": [MODEL]})
    with pytest.raises(ValueError):
        build_view(with_proof(bundle, provenance=provenance))


def test_benchmarks_are_exact_locked_values(bundle):
    assert [(b.version, b.verified, b.total, b.percent) for b in build_view(bundle).benchmarks] == [
        ("v1", 1, 7, "14.29%"), ("v2", 3, 7, "42.86%"), ("v3", 4, 7, "57.14%")]


def test_api_serves_only_recorded_safe_view(monkeypatch):
    monkeypatch.setattr(routes, "_secrets", lambda: ("sk-test-secret-value-000000",))
    response = TestClient(app).get("/api/replay")
    assert response.status_code == 200 and "Content-Security-Policy" in response.headers
    data = response.json()
    assert set(data) == set(ReplayView.model_fields) and data["mode"] == "RECORDED"
    assert data["mode_label"].startswith("RECORDED") and "live" not in response.text.lower()
    assert unsafe_fragments(response.text) == []
    for forbidden in ("messages", "raw_response", "stdout_excerpt", "stderr_excerpt", "request_options", "NEBIUS"):
        assert forbidden not in response.text


@pytest.mark.parametrize("content", [
    None, "{not json", '{"provenance": {}}',
    "SECRET", "HOSTPATH",
])
def test_missing_malformed_or_unsafe_evidence_fails_safe(tmp_path, monkeypatch, content):
    path = tmp_path / "bundle.json"
    original = routes.BUNDLE_PATH.read_text(encoding="utf-8")
    if content == "SECRET":
        content = original.replace("Function body", "Function sk-test-secret-value-000000 body")
    elif content == "HOSTPATH":
        content = original.replace("Function body", "Function C:\\Users\\someone\\project\\x.py body")
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(routes, "BUNDLE_PATH", path)
    monkeypatch.setattr(routes, "_secrets", lambda: ("sk-test-secret-value-000000",))
    response = TestClient(app).get("/api/replay")
    assert response.status_code == 503
    assert response.json() == {"state": "evidence_unavailable", "reason": "Recorded evidence is missing or failed validation."}
    assert str(tmp_path) not in response.text


def test_page_is_static_recorded_and_script_safe():
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200 and "script-src 'self'" in page.headers["Content-Security-Policy"]
    html = page.text
    assert '<script src="/static/app.js" defer></script>' in html and html.count("<script") == 1
    assert "Replay demo" in html and "not part of" in html
    js = client.get("/static/app.js").text
    assert "REPLAYING RECORDED EVIDENCE" in js and "LIVE" not in js and "LIVE" not in html
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert "innerHTML" not in code and "eval(" not in code and "new Function" not in code
    assert client.get("/static/app.css").status_code == 200


def sources(bundle: ReplayBundle, **overrides) -> dict[str, bytes]:
    proof = bundle.proof_of_fix
    attempts = [{"attempt_number": a.attempt_number,
                 "proposal_attempt": {"model": a.proposal_model, "protocol_retry_count": 0, "protocol_events": []},
                 "verification": {"diff_after_apply": a.applied_diff_against_original},
                 "correction_evidence": {"verification_status": "targeted_test_failure"} if a.correction_evidence_supplied else None}
                for a in bundle.attempts]
    correction = bundle.correction.model_dump()
    correction["request_sha256"] = correction.pop("request_sha256")
    data = {
        "proof-of-fix.json": proof.model_dump(mode="json"),
        "repair-run.json": {"run_id": RUN_ID, "attempts": attempts},
        "demo-summary.json": {"run_id": RUN_ID, "proof_status": proof.status,
                              "reproduction": {"buggy_source": bundle.issue.buggy_source}, "correction": correction,
                              "candidate_2_from_original_source": True},
        "model-audit.json": [{"stage": "diagnosis"}, {"stage": "correction", "returned_model": MODEL,
                                                      "request_user_content_sha256": bundle.correction.request_sha256}],
    }
    for key, value in overrides.items():
        data[key] = value(data[key])
    return {name: json.dumps(value).encode("utf-8") for name, value in data.items()}


def test_bundle_builder_selects_and_cross_checks(bundle):
    built = build_bundle(sources(bundle), derived_from="artifacts/hackathon-demo", demo_label=DEMO_LABEL)
    assert built.provenance.run_id == RUN_ID and [a.proposal_model for a in built.attempts] == [DEMO_LABEL, MODEL]
    assert build_view(built).proof.verified
    for name, change in {
        "repair-run.json": lambda run: {**run, "run_id": "other"},
        "model-audit.json": lambda audit: [audit[0], {**audit[1], "request_user_content_sha256": "0" * 64}],
        "demo-summary.json": lambda summary: {**summary, "proof_status": "NOT_VERIFIED"},
    }.items():
        with pytest.raises(ValueError):
            build_bundle(sources(bundle, **{name: change}), derived_from="x", demo_label=DEMO_LABEL)
    withheld = lambda run: {**run, "attempts": [{**run["attempts"][0], "proposal_attempt": {
        **run["attempts"][0]["proposal_attempt"], "model": {"raw_response_redacted": True}}}, run["attempts"][1]]}
    with pytest.raises(ValueError, match="withheld"):
        build_bundle(sources(bundle, **{"repair-run.json": withheld}), derived_from="x", demo_label=DEMO_LABEL)
    with pytest.raises(ValueError, match="unsafe"):
        build_bundle(sources(bundle), derived_from="x", demo_label=DEMO_LABEL, secrets=(bundle.issue.buggy_source[:20],))


def test_local_canonical_hash_check(bundle, tmp_path):
    files = sources(bundle)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    built = build_bundle(files, derived_from="x", demo_label=DEMO_LABEL)
    assert local_canonical_status(built, tmp_path) == "match"
    (tmp_path / "proof-of-fix.json").write_bytes(files["proof-of-fix.json"] + b" ")
    assert local_canonical_status(built, tmp_path) == "mismatch"
    assert local_canonical_status(built, tmp_path / "absent") == "unavailable"
    assert hashlib.sha256(files["repair-run.json"]).hexdigest() == built.provenance.source_sha256["repair-run.json"]


def test_presentation_cannot_reach_repair_engine():
    # Pure report/state models may load; nothing that proposes, verifies, calls a model or runs Docker.
    code = ("import sys, app.main; print(sorted(m for m in sys.modules if m.startswith(("
            "'app.agent.orchestrator', 'app.agent.repair', 'app.agent.verification', 'app.agent.recovery', "
            "'app.llm', 'app.sandbox.runner', 'app.evaluation', 'openai', 'scripts'))))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
