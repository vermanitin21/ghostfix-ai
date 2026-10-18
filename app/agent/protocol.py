"""Pure, fail-closed protocol helpers. No source writes, execution or model calls."""
import re
from pathlib import Path

from app.agent.patches import validate_patch
from app.agent.state import DiagnosisInput, PatchLimits, PatchProposal
from app.tools.files import redact


def format_retry_witness(proposal: PatchProposal, context: DiagnosisInput, root: Path,
                         limits: PatchLimits) -> PatchProposal | None:
    """Prove intent only for a missing terminal LF or an exact Markdown envelope.

The witness is NOT accepted/applied. A model must re-encode it and pass strict
validation; its computed effects must equal the witness. Never guess missing
edits, hunk counts, paths or context. A strict whole-patch check catches hazards
after an earlier format error too.
    """
    diff = proposal.unified_diff
    if len(diff.encode("utf-8")) > limits.max_diff_bytes:
        return None
    if diff.startswith("```diff\n") and diff.endswith("\n```\n"):
        candidate = diff[len("```diff\n"):-len("```\n")]
    elif not diff.endswith("\n"):
        candidate = diff + "\n"
    else:
        return None
    witness = proposal.model_copy(update={"unified_diff": candidate})
    return witness if validate_patch(witness, context, root, limits).valid else None


def safe_protocol_record(value, secrets: tuple[str, ...]):
    """Preserve safe text verbatim; sanitize sensitive raw provenance."""
    if isinstance(value, str):
        value = redact(value, secrets)
        value = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+|\\\\[^\s\"'<>]+", "[host-path]", value)
        return re.sub(r"(?<![\w.])/(?!/)[A-Za-z0-9_.-]+(?:/[^\s\"'<>]*)?", "[absolute-path]", value)
    if isinstance(value, list):
        return [safe_protocol_record(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: safe_protocol_record(item, secrets) for key, item in value.items()}
    return value
