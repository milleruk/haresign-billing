"""Who may see and manage an organisation's billing.

One question, asked in one place: given an authenticated session and an
organisation UUID that arrived in a URL, may this request proceed?

The organisation UUID in the URL is **never** the authority. It is a lookup key
whose legitimacy is established by finding a matching, fresh membership captured
from Identity at sign-in. That is the whole defence against cross-organisation
IDOR, and it is why `require_organization_admin` returns the resolved membership
rather than a boolean — a view that has to fetch the organisation itself
afterwards is a view that can fetch the wrong one.
"""

from __future__ import annotations

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

# Role keys that mean "may manage this organisation's billing". Compared exactly
# against what Identity reported; never inferred from a substring, because
# "administrator" as a substring also matches roles that are not one.
ADMIN_ROLE_KEYS = frozenset({'organization_admin', 'owner'})


@dataclass(frozen=True)
class OrganizationAccess:
    """The authorisation decision, and how it was reached."""

    organization_id: str
    organization_name: str
    organization_type: str
    # True when the actor reached this organisation through platform-administrator
    # support access rather than their own membership. Always audited.
    support_access: bool = False
    role: str = ''


def _membership_is_fresh(membership: SessionMembership) -> bool:
    """Has the captured membership aged past what we are willing to rely on?

    An ID token is a snapshot. Somebody removed from an organisation at 10:00 must
    not still be managing its billing at 17:00 because they signed in at 09:00. So
    a membership older than `IDENTITY_MEMBERSHIP_MAX_AGE` is not trusted and the
    request is sent back through Identity, which re-issues a current claim.

    A short maximum age, not a background sync: a background sync would be an
    undocumented runtime dependency on Identity, which the ownership contract
    forbids. Re-authorization goes through the front door the protocol provides.
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


def resolve_organization(request, organization_id) -> OrganizationAccess | None:
    """Resolve a URL-supplied organisation UUID against this session. None if refused.

    Order matters. Membership is checked first, so a platform administrator who is
    genuinely a member of an organisation is treated as a member — support access
    is the exception, not the default, and an audit trail that recorded every
    staff member's own organisation as "support access" would bury the events that
    matter.
    """
    if not request.user.is_authenticated:
        return None

    membership = next(
        (m for m in memberships_for(request) if str(m.organization_id) == str(organization_id)),
        None,
    )

    if membership is not None:
        if not _membership_is_fresh(membership):
            return None
        if not membership.is_administrator and membership.role not in ADMIN_ROLE_KEYS:
            return None
        return OrganizationAccess(
            organization_id=str(membership.organization_id),
            organization_name=membership.organization_name,
            organization_type=membership.organization_type,
            support_access=False,
            role=membership.role,
        )

    if request.user.is_platform_admin:
        # Support access. The organisation is not resolved from a membership, so
        # its display name is not known here; the view reads it from the billing
        # account if one exists. Every use is recorded by the decorator below.
        return OrganizationAccess(
            organization_id=str(organization_id),
            organization_name='',
            organization_type='',
            support_access=True,
            role='platform_admin',
        )

    return None


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

            if access.support_access:
                record(
                    events.SUPPORT_ACCESS_USED,
                    request=request,
                    organization_id=_as_uuid(organization_id),
                    support_access=True,
                    metadata={'path': request.path, 'method': request.method},
                )

            kwargs['access'] = access
            return func(request, *args, **kwargs)

        return _wrapped

    return decorator(view_func) if view_func else decorator


def _as_uuid(value):
    """A UUID for the audit row, or None. A malformed URL must not break auditing."""
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
