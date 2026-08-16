"""Environment readers.

Small on purpose. Configuration is read in exactly one place and with exactly
one set of coercion rules, so "is DEBUG on?" cannot have two answers depending
on which module asked.
"""

import os
from pathlib import Path

_TRUE = {'1', 'true', 'yes', 'on'}


def env_str(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def env_secret(name: str, default: str = '') -> str:
    """Read a secret directly or from ``<NAME>_FILE``, never both."""
    value = os.environ.get(name, '')
    file_name = os.environ.get(f'{name}_FILE', '').strip()
    if value and file_name:
        raise RuntimeError(f'{name} and {name}_FILE cannot both be set.')
    if file_name:
        try:
            return Path(file_name).read_text().strip()
        except OSError as exc:
            raise RuntimeError(f'Unable to read {name}_FILE.') from exc
    return value.strip() or default


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in _TRUE


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        # A misconfigured throttle limit or token lifetime silently falling back
        # to a default is a security control quietly changing value.
        raise RuntimeError(f'{name} must be an integer, got {raw!r}') from exc


def env_list(name: str, default: str = '') -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]
