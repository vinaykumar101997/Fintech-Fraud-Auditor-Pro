"""Retry with exponential backoff for model calls.

Kept free of LangChain and provider imports so it can be tested with the
dev-only install.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Substrings identifying failures that a retry cannot fix: bad credentials,
# malformed requests, or a resource that does not exist. Matched against
# "{exception type}: {message}" so it catches both botocore's ClientError,
# which encodes the AWS error code in the message (e.g. "An error occurred
# (ValidationException) when calling..."), and google-api-core's typed
# exceptions, which encode it in the class name (PermissionDenied, NotFound,
# ...). Anything not on this list is treated as transient and retried -
# throttling, timeouts, and 5xx/ServiceUnavailable all fall through to that
# default, which is deliberate: the list only needs to name the failures that
# are certainly terminal, not enumerate every failure that might be transient.
TERMINAL_ERROR_MARKERS = (
    "ValidationException",       # Bedrock: bad model id/params, never transient
    "AccessDenied",              # Bedrock/AWS: missing IAM permission
    "UnauthorizedException",
    "UnrecognizedClientException",  # AWS: invalid access key
    "ExpiredTokenException",
    "InvalidSignatureException",
    "NoCredentialsError",        # boto3: no credentials configured at all
    "ResourceNotFoundException", # Bedrock: model/resource does not exist
    "PermissionDenied",          # Vertex/google-api-core
    "Unauthenticated",           # Vertex/google-api-core
    "InvalidArgument",           # Vertex/google-api-core
    "NotFound",                  # Vertex/google-api-core
)


def is_terminal(exc: Exception) -> bool:
    """True for failures a retry cannot fix, so the caller can fail fast."""
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in TERMINAL_ERROR_MARKERS)


def call_with_retry(
    fn: Callable[[], T],
    description: str,
    *,
    max_retries: int,
    base_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Exponential backoff with jitter, but only for transient failures.

    A terminal failure (bad credentials, a malformed request, a model that
    does not exist) raises immediately - retrying it three times with
    growing delays just delays an error that will not change.

    `max_retries` (LLM_MAX_RETRIES) is the number of retries after the first
    attempt, so fn() is called at most 1 + max_retries times: 0 makes exactly
    one attempt, 3 makes up to four. Negative values are treated as 0. When
    every attempt fails, the last error is raised.
    """
    last: Optional[Exception] = None
    attempts = 1 + max(max_retries, 0)
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - provider raises many types
            last = exc
            if is_terminal(exc):
                logger.error("%s failed with a terminal error: %s. Not retrying.",
                             description, exc)
                raise
            if attempt == attempts - 1:
                break
            delay = base_delay * (2**attempt) + random.uniform(0, 0.4)
            logger.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %.1fs.",
                description, attempt + 1, attempts, exc, delay,
            )
            sleep(delay)
    raise last  # type: ignore[misc]
