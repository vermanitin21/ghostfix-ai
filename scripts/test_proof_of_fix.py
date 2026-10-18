"""Real self-correction -> in-memory RepairRun -> deterministic proof artifacts."""
import asyncio
import json
import logging
import re
from pathlib import Path

from app.core.config import get_settings
from app.reporting import build_proof_of_fix, render_json, render_markdown
from scripts.test_self_correction import run_demo


def check_output(text: str, secret: str) -> None:
    # Inspect decoded JSON strings: a colon followed by an escaped newline is not
    # a Windows drive path. Markdown is already plain text.
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, (dict, list)):
        def visit(item):
            if isinstance(item, str):
                check_output(item, secret)
            elif isinstance(item, dict):
                for key, child in item.items():
                    check_output(key, secret)
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(value)
        return
    if (secret and secret in text) or "NEBIUS_API_KEY" in text:
        raise ValueError("Report contains credential material")
    if re.search(r"[A-Za-z]:[\\/]|ghostfix-verify-|/tmp/|/workspace|/Users/", text):
        raise ValueError("Report contains a host or Docker workspace path")


async def verify() -> int:
    run, audit = await run_demo()
    key = get_settings().nebius_api_key.get_secret_value()
    # Direct object handoff: no intermediate/synthetic repair-run.json.
    proof = build_proof_of_fix(run, secrets=(key,))
    json_text, markdown = render_json(proof), render_markdown(proof)
    check_output(json_text, key)
    check_output(markdown, key)
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "proof-of-fix.json").write_text(json_text + "\n", encoding="utf-8")
    (artifacts / "proof-of-fix.md").write_text(markdown, encoding="utf-8")
    audit.update({"proof_status": proof.status, "proof_verified": proof.verified,
                  "reports_secret_check": "passed", "reports_host_path_check": "passed",
                  "artifacts": ["artifacts/proof-of-fix.json", "artifacts/proof-of-fix.md"]})
    print(json.dumps(audit, indent=2).replace(key, "[REDACTED]"))
    return 0 if audit["verified"] and proof.verified else 1


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(verify())
    except Exception as error:
        # Only exception class and local frame identity; never remote error bodies.
        trace = error.__traceback__
        while trace.tb_next is not None:
            trace = trace.tb_next
        print(json.dumps({"verified": False, "error": "Proof E2E failed; no success inferred",
                          "error_type": type(error).__name__,
                          "stage": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
