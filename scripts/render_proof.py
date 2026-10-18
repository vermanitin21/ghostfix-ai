"""Render an existing trusted RepairRun JSON without running repair or tests."""
import argparse
import sys
from pathlib import Path

from app.agent.state import RepairRun
from app.core.config import get_settings
from app.reporting import build_proof_of_fix, render_json, render_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json", type=Path)
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()
    try:
        with args.run_json.open("rb") as source:
            data = source.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("Run input too large")
        run = RepairRun.model_validate_json(data)
        key = get_settings().nebius_api_key
        proof = build_proof_of_fix(run, secrets=(key.get_secret_value(),) if key else ())
        print(render_json(proof) if args.format == "json" else render_markdown(proof))
        return 0
    except (ValueError, OSError):
        print("Unable to render proof: check the input RepairRun JSON and configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
