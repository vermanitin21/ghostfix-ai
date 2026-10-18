"""Manual connectivity check: python -m scripts.test_nebius."""

import asyncio
import logging
import sys

from openai import APIError, APIStatusError, AuthenticationError

from app.core.config import get_settings
from app.llm.nebius_client import NebiusClient

EXPECTED_RESPONSE = "GHOSTFIX_NEBIUS_OK"


async def smoke_test() -> int:
    settings = get_settings()
    client = NebiusClient(settings)
    try:
        async with asyncio.timeout(60):
            result = await client.complete_with_metadata(
                [{"role": "user", "content": f"Reply exactly: {EXPECTED_RESPONSE}"}]
            )
    finally:
        await client.close()

    # Redact even if a remote response unexpectedly contains the credential.
    secret = settings.nebius_api_key.get_secret_value()
    print(f"Model: {result.model.replace(secret, '[REDACTED]')}")
    print(f"Response: {result.content.replace(secret, '[REDACTED]')}")
    if result.content.strip() != EXPECTED_RESPONSE:
        print("Unexpected response: exact confirmation was not returned.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    # SDK debug logging can include HTTP headers; this standalone check suppresses it.
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(smoke_test())
    except AuthenticationError:
        message = "Nebius authentication failed. Check NEBIUS_API_KEY."
    except APIStatusError as error:
        message = f"Nebius API request failed (HTTP {error.status_code})."
    except APIError:
        message = "Nebius API connection or response failed."
    except TimeoutError:
        message = "Nebius request exceeded the 60-second timeout."
    except Exception:
        # Avoid raw exceptions: settings validation and API bodies may contain secrets.
        message = "Connectivity check failed. Check settings, model ID, and response format."
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
