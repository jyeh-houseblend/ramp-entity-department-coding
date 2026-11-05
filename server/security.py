"""Ramp webhook signature verification.

Ramp sends X-Ramp-Signature: the hex HMAC-SHA256 of the raw request body,
keyed with the subscription's secret. Always verify the raw bytes, never a
re-serialized JSON body.
"""
from __future__ import annotations

import hashlib
import hmac


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """True only if a signature is present and matches (constant-time)."""
    if not secret or not signature:
        return False
    return hmac.compare_digest(compute_signature(raw_body, secret), signature.strip())
