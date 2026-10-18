"""Raw-evidence fidelity policy, ported verbatim from the benchmark audit tooling.

Never rewrite payload text; withhold unsafe text explicitly with its exact hash.
"""
import hashlib
import json
import re
from pathlib import Path

# Boundaries exclude identifiers such as foo:\\test. Require directory structure
# or a filename extension, not a bare JSON escape such as t:\\n.
WINDOWS = re.compile(r'''(?<![\w])(?:[A-Za-z]:[\\/](?:[^\s\\/"'<>]+[\\/])+[^\s"'<>]*|[A-Za-z]:[\\/][^\s\\/"'<>]+\.[A-Za-z0-9]+|\\\\[A-Za-z0-9_.-]+\\[^\s"'<>]+)''')
POSIX = re.compile(r'''(?<![\w.])/(?:tmp|home|Users|var|workspace|work|root|mnt)(?:/[^\s"'<>]*)?(?=$|[\s"'<>])''')
CREDENTIAL = re.compile(r'(?im)(?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[:=]|-----BEGIN [^-]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9_.-]{12,}|\bsk-[A-Za-z0-9_-]{16,}')

def sha(text):
    """Hash exact UTF-8 text, no newline or Unicode normalization."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def reasons(text, secrets=(), depth=0):
    found = set()
    if any(secret and secret in text for secret in secrets):
        found.add('known_credential')
    if CREDENTIAL.search(text):
        found.add('credential_pattern')
    # Decode valid JSON for inspection ONLY. Store the original string unchanged.
    try:
        decoded = json.loads(text) if depth < 8 else None
    except (ValueError, RecursionError):
        decoded = None
    if isinstance(decoded, (dict, list, str)) and decoded != text:
        def visit(value):
            if isinstance(value, str):
                found.update(reasons(value, secrets, depth + 1))
            elif isinstance(value, dict):
                for k, v in value.items():
                    visit(k)
                    visit(v)
            elif isinstance(value, list):
                for v in value:
                    visit(v)
        visit(decoded)
    elif WINDOWS.search(text) or POSIX.search(text):
        found.add('absolute_host_path')
    return sorted(found)

def raw_record(text, secrets=()):
    why = reasons(text, secrets)
    return {'raw_response_sha256': sha(text), 'raw_response_redacted': bool(why),
            'redaction_reason': why, 'raw_response_text': None if why else text,
            'stored_text_sha256': None if why else sha(text),
            'hash_rule': 'exact UTF-8; preserve Unicode and CRLF/LF; no normalization'}

def sanitize(value, secrets=()):
    if isinstance(value, str):
        return raw_record(value, secrets) if reasons(value, secrets) else value
    if isinstance(value, dict):
        # Unsafe keys cannot be silently relabeled or collide with other keys.
        if any(reasons(str(k), secrets) for k in value):
            return raw_record(json.dumps(value, ensure_ascii=False), secrets)
        return {k: sanitize(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    return value
