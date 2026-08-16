from __future__ import annotations

import hashlib
import json

from django.utils.crypto import salted_hmac


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def identity_digest(value, purpose: str) -> str:
    payload = value if isinstance(value, bytes) else canonical_json(value)
    return salted_hmac(f'billing_migration.{purpose}', payload, algorithm='sha256').hexdigest()


def artifact_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
