"""Display names for organisation UUIDs, fetched from Identity and cached.

A UUID is not a label. Every page here is *about* an organisation, and showing
`3f3e088b-e5c9-…` where a practice name belongs makes a correct page unusable.

What this is not
----------------

**It is not an authorization input, ever.** Whether somebody may see an
organisation is decided by their memberships and by the graph's edges; this
module only decides what to *call* the organisation once that decision has
already been made. Nothing here is consulted before an access check, and
`identity/tests/test_display.py` asserts that a page refused without a name is
still refused with one.

**It is not a copy of Identity's directory.** Names are fetched for the
identifiers this service already holds, one request at a time, and Identity's
endpoint has no listing route to copy even if we wanted one. What is stored is a
label cache keyed by a UUID we were already entitled to hold.

Failure is cosmetic by design
-----------------------------

If Identity is unreachable, or the credential is wrong, or the organisation is
unknown, the answer is a neutral fallback label and the page renders. A billing
page that 500s because a *name* could not be fetched would turn a cosmetic
dependency into an outage, and the fallback is honest rather than misleading: it
says "Organisation", not somebody else's name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .graph_models import OrganizationDisplayName

logger = logging.getLogger('haresign.billing')

_HTTP_TIMEOUT = 5
SCHEME = 'Haresign-Service'
# Identity caps one request; ask for no more than it will answer.
MAX_PER_REQUEST = 200


class DisplayUnavailable(RuntimeError):
    """Identity could not be asked. Callers fall back to a neutral label."""


def _sign(method: str, path: str, digest: str) -> str:
    key_id = settings.IDENTITY_DISPLAY_KEY_ID
    secret = settings.IDENTITY_DISPLAY_SECRET
    stamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), f'{method}\n{path}\n{stamp}\n{digest}'.encode(), hashlib.sha256
    ).hexdigest()
    return f'{SCHEME} {key_id}:{stamp}:{signature}'


def fetch_display_names(organization_ids) -> dict[str, dict]:
    """Ask Identity for names. Raises `DisplayUnavailable` rather than guessing."""
    wanted = [str(value) for value in organization_ids]
    if not wanted:
        return {}

    url = (settings.IDENTITY_DISPLAY_URL or '').strip()
    if not url:
        raise DisplayUnavailable('No IDENTITY_DISPLAY_URL is configured.')
    if not settings.IDENTITY_DISPLAY_KEY_ID or not settings.IDENTITY_DISPLAY_SECRET:
        raise DisplayUnavailable('No organisation-display credential is configured.')

    parsed = urlparse(url)
    if parsed.scheme != 'https' and not settings.OIDC_ALLOW_INSECURE_LOOPBACK:
        raise DisplayUnavailable('The organisation-display endpoint must be served over HTTPS.')

    found: dict[str, dict] = {}
    for start in range(0, len(wanted), MAX_PER_REQUEST):
        batch = wanted[start : start + MAX_PER_REQUEST]
        # The body is signed, so it is built once and both signed and sent —
        # re-serialising would risk signing bytes other than the ones sent.
        body = json.dumps({'organization_ids': batch}).encode()
        digest = hashlib.sha256(body).hexdigest()
        headers = {
            'Authorization': _sign('POST', parsed.path, digest),
            'Content-Type': 'application/json',
        }
        try:
            response = requests.post(url, data=body, headers=headers, timeout=_HTTP_TIMEOUT)
        except Exception as exc:
            raise DisplayUnavailable('Unable to reach the organisation-display endpoint.') from exc
        if response.status_code != 200:
            raise DisplayUnavailable(
                f'The organisation-display endpoint refused (HTTP {response.status_code}).'
            )
        try:
            document = response.json()
        except Exception as exc:
            raise DisplayUnavailable('The organisation-display response was not JSON.') from exc
        for record in document.get('organizations', []):
            identifier = str(record.get('organization_id', ''))
            if identifier:
                found[identifier] = {
                    'display_name': record.get('display_name', ''),
                    'organization_type': record.get('organization_type', ''),
                }
    return found


def _store(found: dict[str, dict]) -> None:
    now = timezone.now()
    with transaction.atomic():
        for identifier, record in found.items():
            OrganizationDisplayName.objects.update_or_create(
                organization_id=identifier,
                defaults={
                    'display_name': record['display_name'][:255],
                    'organization_type': record['organization_type'][:32],
                    'fetched_at': now,
                },
            )


def names_for(organization_ids, *, refresh: bool = True) -> dict[str, str]:
    """`{uuid: display name}` for the identifiers given.

    Held names are used first; anything missing or past its age is fetched. A
    failed fetch is logged and leaves the held names in place — a stale label is
    better than no page.
    """
    wanted = {str(value) for value in organization_ids if value}
    if not wanted:
        return {}

    held = {
        str(row.organization_id): row
        for row in OrganizationDisplayName.objects.filter(organization_id__in=wanted)
    }
    max_age = settings.IDENTITY_DISPLAY_MAX_AGE
    cutoff = timezone.now() - timezone.timedelta(seconds=max_age) if max_age else None
    stale = {
        identifier
        for identifier in wanted
        if identifier not in held or (cutoff and held[identifier].fetched_at < cutoff)
    }

    if refresh and stale:
        try:
            found = fetch_display_names(stale)
        except DisplayUnavailable as exc:
            # Cosmetic. The page renders with what we hold, or with a fallback.
            logger.warning('organisation display: %s', exc)
        else:
            _store(found)
            for identifier, record in found.items():
                held[identifier] = OrganizationDisplayName(
                    organization_id=identifier,
                    display_name=record['display_name'],
                    organization_type=record['organization_type'],
                )

    return {identifier: row.display_name for identifier, row in held.items() if row.display_name}


def label_for(organization_id, names: dict[str, str] | None = None, *, fallback='Organisation'):
    """One organisation's label, never its UUID.

    A UUID is not a name, and showing one to somebody who asked "which practice
    is this" is worse than admitting we do not currently know.
    """
    identifier = str(organization_id)
    if names is None:
        names = names_for([identifier])
    return names.get(identifier) or fallback
