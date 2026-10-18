"""Derive the tracked, sanitized replay bundle from local canonical demo evidence.

Read-only with respect to artifacts/. No model, network or Docker call. Refuses to
overwrite an existing bundle unless --replace is given.

    .\\.venv\\Scripts\\python.exe scripts/build_replay_bundle.py
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.presentation.bundle import SOURCE_FILES, build_bundle
from app.presentation.routes import BUNDLE_PATH
from scripts.run_self_correction_demo import DEMO_MODEL  # authoritative injector label

CANONICAL = ROOT / "artifacts" / "hackathon-demo"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="store_true", help="overwrite an existing bundle")
    args = parser.parse_args()
    if BUNDLE_PATH.exists() and not args.replace:
        print(f"Refusing to overwrite {BUNDLE_PATH.relative_to(ROOT)}; pass --replace to regenerate.")
        return 2
    try:
        source = {name: (CANONICAL / name).read_bytes() for name in SOURCE_FILES}
    except OSError:
        print("Canonical evidence not found under artifacts/hackathon-demo; nothing written.")
        return 1
    key = get_settings().nebius_api_key
    secrets = (key.get_secret_value(),) if key and key.get_secret_value() else ()
    try:
        bundle = build_bundle(source, derived_from="artifacts/hackathon-demo", demo_label=DEMO_MODEL, secrets=secrets)
    except ValueError as error:
        print(f"Bundle not written: {error}")
        return 1
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {BUNDLE_PATH.relative_to(ROOT)} for run {bundle.provenance.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
