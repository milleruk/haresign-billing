"""The versioned internal entitlement API.

One question, one answer: what products does this Identity organisation
effectively hold right now?

**What the response contains**: the organisation UUID, stable product keys,
whether each is held, when each is currently known to end, and version/freshness
metadata.

**What it must never contain**, and there are tests asserting each absence: a
plan price, an amount, an invoice, a provider customer or subscription id, a
billing contact, an email address, a person's name, or anything about how the
entitlement was paid for. Haresign Intelligence needs to know whether a tool
opens. It does not need to know what the practice pays, and giving it that would
spread financial data across a second system for no gain.

**Caching and unavailability** are contract, not implementation detail, so they
are stated in the response: `cache_max_age` says how long the answer may be held,
and consumers are documented to **fail closed** when Billing is unreachable or the
cached answer has expired. A paid feature that opens because the billing service
is down is not a paid feature.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from audit import events
from audit.services import record
from billing.entitlements import entitlements_for_organization
from web.throttling import Throttled, throttle

from .auth import ServiceAuthError, authenticate

logger = logging.getLogger('haresign.billing')

API_VERSION = 'v1'
# Bumped when the *shape* changes. Consumers pin it, so an additive field is a
# revision and a removed field is a new path.
SCHEMA_REVISION = 1


def _refused(request, reason: str, status: int = 401) -> JsonResponse:
    record(events.ENTITLEMENT_API_REJECTED, request=request, metadata={'reason': reason})
    # One generic body for every refusal. Distinguishing "unknown key" from "bad
    # signature" from "unknown organisation" would make this endpoint an oracle.
    return JsonResponse({'error': 'unauthorized'}, status=status)


@require_GET
@never_cache
def organization_entitlements(request, organization_id):
    """`GET /api/v1/organizations/<uuid>/entitlements/`"""
    try:
        throttle(request, 'entitlement_api')
    except Throttled:
        return JsonResponse({'error': 'rate_limited'}, status=429)

    try:
        key_id = authenticate(request)
    except ServiceAuthError as exc:
        return _refused(request, str(exc))

    try:
        organization_uuid = uuid.UUID(str(organization_id))
    except (ValueError, TypeError):
        # A malformed UUID is a client bug, not an authorisation failure, and
        # saying so costs nothing — the caller is already authenticated.
        return JsonResponse({'error': 'invalid_organization_id'}, status=400)

    try:
        result = entitlements_for_organization(organization_uuid)
    except Exception:
        logger.exception('entitlement api: derivation failed')
        # Fail closed, loudly. A 503 tells the consumer to refuse the feature;
        # an empty 200 would read as "this organisation holds nothing", which is
        # the same outcome but indistinguishable from a genuine answer and
        # therefore cacheable. It must not be cached.
        return JsonResponse({'error': 'unavailable'}, status=503)

    record(
        events.ENTITLEMENT_QUERIED,
        request=request,
        organization_id=organization_uuid,
        metadata={'key_id': key_id, 'products': len(result.products)},
    )

    body = {
        'api_version': API_VERSION,
        'schema_revision': SCHEMA_REVISION,
        'organization_id': str(organization_uuid),
        'evaluated_at': (result.evaluated_at or timezone.now()).isoformat(),
        'cache_max_age': settings.ENTITLEMENT_CACHE_SECONDS,
        'products': [
            {
                'product_key': entry.product_key,
                'entitled': entry.entitled,
                'effective_until': (
                    entry.effective_until.isoformat() if entry.effective_until else None
                ),
            }
            for entry in sorted(result.products.values(), key=lambda e: e.product_key)
        ],
    }
    response = JsonResponse(body)
    # The consumer's cache, not a shared one. A proxy holding an entitlement
    # answer would serve one organisation's state to another's request.
    response.headers['Cache-Control'] = f'private, max-age={settings.ENTITLEMENT_CACHE_SECONDS}'
    return response


@require_GET
@never_cache
def catalogue(request):
    """`GET /api/v1/products/` — the stable product keys a consumer may ask about.

    Exists so Intelligence can validate its own configuration at deploy time
    rather than discovering a typo'd product key as a permanently-closed feature.
    """
    from catalog.models import Product

    try:
        throttle(request, 'entitlement_api')
    except Throttled:
        return JsonResponse({'error': 'rate_limited'}, status=429)

    try:
        authenticate(request)
    except ServiceAuthError as exc:
        return _refused(request, str(exc))

    return JsonResponse(
        {
            'api_version': API_VERSION,
            'schema_revision': SCHEMA_REVISION,
            'products': [
                {'product_key': key, 'name': name}
                for key, name in Product.objects.filter(is_active=True)
                .order_by('key')
                .values_list('key', 'name')
            ],
        }
    )
