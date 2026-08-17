"""Who may see and manage an organisation's billing.

One question, asked in one place: given an authenticated session and an
organisation UUID that arrived in a URL, may this request proceed?

The organisation UUID in the URL is **never** the authority. It is a lookup key
whose legitimacy is established by finding a matching, fresh membership captured
from Identity at sign-in. That is the whole defence against cross-organisation
IDOR, and it is why `require_organization_admin` returns the resolved membership
rather than a boolean — a view that has to fetch the organisation itself
afterwards is a view that can fetch the wrong one.

**Billing access is organisation-administrator only, and there is no other
route.** An active `organization.admin` of a practice may manage that practice's
billing. An active `organization.admin` of a PCN may manage that PCN's billing,
and may purchase for practices currently linked to it. Everybody else is refused:
ordinary members, pending, rejected and revoked memberships, administrators of
unrelated organisations, and — deliberately — platform administrators.

**Platform administration grants nothing here.** Phase 4A had a support bypass
that let `platform.admin` open any organisation's billing. It is gone, and so is
every dependency on the `haresign_platform_admin` claim, which Identity does not
in fact emit — `docs/architecture.md` over there is explicit that
platform-administrator state is never a claim, so the bypass was reachable only
in the synthetic rehearsal and would have been dead code in production. A
platform administrator who is also an organisation administrator reaches that
organisation through the membership, like anyone else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from audit import events
from audit.services import record

from .models import SessionMembership

# The one role key that means "may manage this organisation's billing", compared
# exactly against what Identity reported.
#
# It is `organization.admin` — dotted, namespaced, and exactly the key in
# `haresign-core`'s `organizations/roles.py`. Phase 4A guessed `organization_admin`
# from a synthetic claim, which matches nothing Identity has ever emitted and
# would have refused every real administrator. Never inferred from a substring:
# "administrator" as a substring also matches roles that are not one.
ADMIN_ROLE_KEYS = frozenset({'organization.admin'})

# Organisation types whose administrators may buy for *other* organisations. Only
# a PCN sponsors; a practice pays for itself and nothing else.
SPONSORING_TYPES = frozenset({'pcn'})


@dataclass(frozen=True)
class OrganizationAccess:
    """The authorisation decision, and how it was reached."""

    organization_id: str
    organization_name: str
    organization_type: str
    role: str = ''

    @property
    def may_sponsor(self) -> bool:
        """May this administrator buy on behalf of another organisation?

        True for a PCN administrator only, and even then the *specific*
        beneficiary is checked against the live organisation graph at the point of
        purchase — this says "a PCN may sponsor", not "this PCN may sponsor that
        practice".
        """
        return self.organization_type in SPONSORING_TYPES


def _membership_is_fresh(membership: SessionMembership) -> bool:
    """Has the captured membership aged past what we are willing to rely on?

    An ID token is a snapshot. Somebody removed from an organisation at 10:00 must
    not still be managing its billing at 17:00 because they signed in at 09:00. So
    a membership older than `IDENTITY_MEMBERSHIP_MAX_AGE` is not trusted and the
    request is sent back through Identity, which re-issues a current claim.

    A short maximum age, not a background sync: a background sync would be an
    undocumented runtime dependency on Identity's membership tables, which the
    ownership contract forbids. Re-authorization goes through the front door the
    protocol provides.
    """
    age = (timezone.now() - membership.captured_at).total_seconds()
    return age <= settings.IDENTITY_MEMBERSHIP_MAX_AGE


def memberships_for(request) -> list[SessionMembership]:
    """Every organisation this session may act for, freshest first."""
    if not request.user.is_authenticated or not request.session.session_key:
        return []
    return list(
        SessionMembership.objects.filter(session_key=request.session.session_key, user=request.user)
    )


def administered_memberships(request) -> list[SessionMembership]:
    """Only the organisations this session may *administer*, and only fresh ones.

    What the organisation picker offers. Offering one the authorization check
    would refuse produces a menu whose items 404, which reads as a broken product
    rather than as a boundary.
    """
    return [
        membership
        for membership in memberships_for(request)
        if membership.is_administrator and _membership_is_fresh(membership)
    ]


def resolve_organization(request, organization_id) -> OrganizationAccess | None:
    """Resolve a URL-supplied organisation UUID against this session. None if refused.

    There is exactly one way through: a fresh, active, administrator membership of
    that organisation. No role, no flag and no claim short-circuits it.
    """
    if not request.user.is_authenticated:
        return None

    membership = next(
        (m for m in memberships_for(request) if str(m.organization_id) == str(organization_id)),
        None,
    )
    if membership is None:
        return None

    if not _membership_is_fresh(membership):
        return None

    # Both conditions, and they are not redundant: `is_administrator` is what was
    # derived at sign-in, `role` is what Identity actually said. A row where they
    # disagree is a bug, and the safe reading of a bug is refusal.
    if not membership.is_administrator or membership.role not in ADMIN_ROLE_KEYS:
        return None

    return OrganizationAccess(
        organization_id=str(membership.organization_id),
        organization_name=membership.organization_name,
        organization_type=membership.organization_type,
        role=membership.role,
    )


def require_organization_admin(view_func=None, *, api: bool = False):
    """Gate a view on administering the named organisation.

    The view receives `access` as a keyword argument and must use
    `access.organization_id` rather than re-reading the URL kwarg — the two are
    the same string today, and the day they are not is the day the URL won.

    A refusal is a 404, not a 403. A 403 confirms the organisation exists, which
    turns this endpoint into an oracle for enumerating customer UUIDs.
    """

    def decorator(func):
        @wraps(func)
        def _wrapped(request, *args, **kwargs):
            organization_id = kwargs.get('organization_id')

            if not request.user.is_authenticated:
                if api:
                    return JsonResponse({'error': 'authentication_required'}, status=401)
                return redirect(f'{reverse("identity:login")}?next={quote(request.path)}')

            access = resolve_organization(request, organization_id)
            if access is None:
                record(
                    events.ACCESS_REFUSED,
                    request=request,
                    organization_id=_as_uuid(organization_id),
                    metadata={'path': request.path},
                )
                raise Http404

            kwargs['access'] = access
            return func(request, *args, **kwargs)

        return _wrapped

    return decorator(view_func) if view_func else decorator


def _as_uuid(value):
    """A UUID for the audit row, or None. A malformed URL must not break auditing."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
