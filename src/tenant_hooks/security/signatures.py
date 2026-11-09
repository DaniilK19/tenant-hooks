"""The webhook signature scheme.

X-Hooks-Signature: v1=<hex HMAC-SHA256 of "{timestamp}.{delivery_id}.{raw body}">

The timestamp is digits only and delivery ids cannot contain a dot (the intake route
enforces both), so a signed message has exactly one way to be split into its fields.
"""

import hashlib
import hmac
from collections.abc import Iterable


def sign(secret: bytes, timestamp: str, delivery_id: str, body: bytes) -> str:
    message = f"{timestamp}.{delivery_id}.".encode() + body
    return "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify(
    secrets: Iterable[bytes], timestamp: str, delivery_id: str, body: bytes, signature: str
) -> bool:
    """Return True if any active secret produced the signature. Several allow rotation."""
    # Compared as bytes: compare_digest raises TypeError on non-ASCII str input,
    # and this value comes straight from a request header.
    provided = signature.encode()
    return any(
        hmac.compare_digest(sign(secret, timestamp, delivery_id, body).encode(), provided)
        for secret in secrets
    )


def timestamp_is_fresh(timestamp: int, now: float, tolerance_seconds: int) -> bool:
    return abs(now - timestamp) <= tolerance_seconds
