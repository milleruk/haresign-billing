from __future__ import annotations

import base64
import json
import os
import stat
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .digests import canonical_json

MAGIC = b'HSBILLING-MIGRATION\x00'
NONCE_BYTES = 12
KEY_BYTES = 32


class ArtifactError(ValueError):
    pass


def load_protected_key(path: str | os.PathLike) -> bytes:
    key_path = Path(path)
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:
        raise ArtifactError('Migration key files must not be accessible by group or others.')
    raw = key_path.read_bytes().strip()
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise ArtifactError('The migration key file is not valid URL-safe base64.') from exc
    if len(decoded) != KEY_BYTES:
        raise ArtifactError('The migration key must decode to exactly 32 bytes.')
    return decoded


def generate_key_file(path: str | os.PathLike) -> None:
    destination = Path(path)
    if destination.exists():
        raise ArtifactError('Refusing to replace an existing migration key file.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(base64.urlsafe_b64encode(os.urandom(KEY_BYTES)))
        stream.write(b'\n')


def encrypt_payload(payload: dict, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    plaintext = canonical_json(payload)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    return MAGIC + struct.pack('!B', NONCE_BYTES) + nonce + ciphertext


def decrypt_payload(data: bytes, key: bytes) -> dict:
    if not data.startswith(MAGIC):
        raise ArtifactError('The file is not a Haresign Billing migration artifact.')
    offset = len(MAGIC)
    nonce_size = data[offset]
    if nonce_size != NONCE_BYTES:
        raise ArtifactError('The migration artifact encryption format is unsupported.')
    nonce = data[offset + 1 : offset + 1 + nonce_size]
    ciphertext = data[offset + 1 + nonce_size :]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
        payload = json.loads(plaintext)
    except Exception as exc:
        raise ArtifactError('Migration artifact authentication failed.') from exc
    if not isinstance(payload, dict):
        raise ArtifactError('Migration artifact root must be an object.')
    return payload


def write_encrypted_artifact(path: str | os.PathLike, payload: dict, key: bytes) -> bytes:
    destination = Path(path)
    if destination.exists():
        raise ArtifactError('Refusing to replace an existing migration artifact.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    encrypted = encrypt_payload(payload, key)
    temporary = destination.with_name(f'.{destination.name}.{os.getpid()}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return encrypted
