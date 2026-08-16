"""Sign-in, callback and sign-out.

Three endpoints and no forms. Everything the browser is trusted with is a state
parameter and a code; everything else — verifier, nonce, the pending request, the
resulting memberships — is server-side session state.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import login, logout
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from audit import events
from audit.services import record
from web.throttling import Throttled, throttle

from .client import (
    AuthorizationRequest,
    OIDCError,
    build_authorization_request,
    end_session_url,
    exchange_code,
    fetch_userinfo,
    validate_id_token,
)
from .models import IdentityUser, SessionMembership

logger = logging.getLogger('haresign.billing')

PENDING_KEY = 'oidc_pending'
# A session *key name*, not a token. S105 flags the word.
ID_TOKEN_KEY = 'oidc_id_token'  # noqa: S105


def _safe_next(raw: str) -> str:
    """Only a same-site path is ever followed after sign-in.

    An open redirect on a login callback is how a phishing page gets to sit behind
    a legitimate hostname. A value with a scheme, a host, or a leading `//` is
    discarded rather than sanitised — sanitising a URL is a game nobody wins.
    """
    if not raw or not raw.startswith('/') or raw.startswith('//'):
        return '/'
    parsed = urlparse(raw)
    return raw if not parsed.scheme and not parsed.netloc else '/'


@require_GET
@never_cache
def login_view(request):
    """Begin an authorization request and redirect to Haresign Identity."""
    if not settings.OIDC_ENABLED:
        return render(request, 'identity/unavailable.html', status=503)

    try:
        throttle(request, 'oidc_login')
    except Throttled:
        return render(request, 'web/429.html', status=429)

    next_url = _safe_next(request.GET.get('next', ''))

    try:
        redirect_url, pending = build_authorization_request(next_url)
    except OIDCError:
        logger.exception('oidc: could not build an authorization request')
        return render(request, 'identity/unavailable.html', status=503)

    # Server-side. The verifier in particular must never reach the browser: with
    # it, a leaked authorization code becomes redeemable.
    request.session[PENDING_KEY] = asdict(pending)
    request.session.modified = True
    return redirect(redirect_url)


@require_GET
@never_cache
def callback_view(request):
    """Complete the flow: validate everything, then start a session."""
    if not settings.OIDC_ENABLED:
        return render(request, 'identity/unavailable.html', status=503)

    try:
        throttle(request, 'oidc_callback')
    except Throttled:
        return render(request, 'web/429.html', status=429)

    stored = request.session.pop(PENDING_KEY, None)
    request.session.modified = True
    if not stored:
        return _reject(request, 'no_pending_request')

    pending = AuthorizationRequest(**stored)

    if (timezone.now().timestamp() - pending.created_at) > settings.OIDC_STATE_TTL:
        return _reject(request, 'authorization_request_expired')

    if error := request.GET.get('error'):
        # The provider refused. `error` is a fixed OAuth code, safe to record;
        # `error_description` is provider-controlled free text and is not.
        return _reject(request, f'provider_error:{error[:64]}')

    state = request.GET.get('state', '')
    if not secrets.compare_digest(state, pending.state):
        return _reject(request, 'state_mismatch')

    code = request.GET.get('code', '')
    if not code:
        return _reject(request, 'no_code')

    try:
        tokens = exchange_code(code, pending)
        claims = validate_id_token(tokens['id_token'], pending)
    except OIDCError as exc:
        return _reject(request, str(exc)[:200])

    userinfo = {}
    if tokens.get('access_token'):
        userinfo = fetch_userinfo(tokens['access_token'])

    merged = {**claims, **userinfo}
    subject = str(merged.get('sub') or '')
    if not subject:
        return _reject(request, 'no_subject')

    user = _upsert_user(subject, merged)
    if not user.is_active:
        return _reject(request, 'account_inactive')

    # Cycle the session key before anything is written against it. Without this a
    # session fixated before sign-in survives it.
    request.session.cycle_key()
    login(request, user, backend='identity.backends.IdentityOIDCBackend')

    _store_memberships(request, user, merged)
    # Held only so sign-out can end the Identity session too.
    request.session[ID_TOKEN_KEY] = tokens['id_token']

    record(
        events.SESSION_STARTED,
        request=request,
        actor_user_id=user.identity_user_id,
        metadata={
            'memberships': SessionMembership.objects.filter(
                session_key=request.session.session_key
            ).count()
        },
    )
    return redirect(_safe_next(pending.next_url))


def _reject(request, reason: str):
    record(events.OIDC_VALIDATION_FAILED, request=request, metadata={'reason': reason})
    logger.warning('oidc: callback rejected (%s)', reason)
    # A generic page. Telling an attacker precisely which check failed is free
    # reconnaissance; the audit row carries the detail for us.
    return render(request, 'identity/sign_in_failed.html', status=400)


def _upsert_user(subject: str, claims: dict) -> IdentityUser:
    """Create or refresh the local shell for an authenticated person."""
    user, created = IdentityUser.objects.get_or_create(
        identity_user_id=subject,
        defaults={
            'display_name': (claims.get('name') or '')[:200],
            'email': (claims.get('email') or '')[:254],
        },
    )
    if created:
        user.set_unusable_password()

    user.display_name = (claims.get('name') or user.display_name)[:200]
    user.email = (claims.get('email') or user.email)[:254]
    # Refreshed from the claim on every sign-in, in both directions: somebody who
    # has *lost* platform-administrator status must lose support access here at
    # their next sign-in, not keep it because the flag was only ever set to True.
    user.is_platform_admin = bool(
        claims.get('haresign_platform_admin') or claims.get('platform_admin')
    )
    user.last_login = timezone.now()
    user.save(
        update_fields=['display_name', 'email', 'is_platform_admin', 'last_login', 'password']
    )
    return user


def _store_memberships(request, user: IdentityUser, claims: dict) -> None:
    """Record the memberships Identity reported, scoped to this session.

    Anything the claim does not explicitly say is an administrator role is not
    treated as one. A membership with an unrecognised role is stored — so support
    can see it — with `is_administrator` False.
    """
    from .authorization import ADMIN_ROLE_KEYS

    session_key = request.session.session_key
    SessionMembership.objects.filter(session_key=session_key).delete()

    raw = claims.get('haresign_memberships') or claims.get('memberships') or []
    if not isinstance(raw, list):
        return

    now = timezone.now()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        organization_id = entry.get('organization_id') or entry.get('id')
        if not organization_id:
            continue
        role = str(entry.get('role') or '')[:64]
        try:
            SessionMembership.objects.create(
                session_key=session_key,
                user=user,
                organization_id=organization_id,
                organization_name=str(entry.get('name') or '')[:255],
                organization_type=str(entry.get('type') or '')[:20],
                role=role,
                is_administrator=role in ADMIN_ROLE_KEYS,
                captured_at=now,
            )
        except Exception:
            # A malformed entry — a bad UUID, a duplicate organisation — must not
            # cost the person their whole sign-in. It costs them that one
            # organisation, which is visible to them immediately.
            logger.warning('oidc: skipped a malformed membership claim')


@require_POST
@never_cache
def logout_view(request):
    """End the local session, then the Identity one.

    POST only. A GET sign-out is fired by any prefetch, image tag or link scanner
    pointed at the URL.
    """
    id_token = request.session.get(ID_TOKEN_KEY, '')
    actor = getattr(request.user, 'identity_user_id', None)
    session_key = request.session.session_key

    if session_key:
        SessionMembership.objects.filter(session_key=session_key).delete()
    logout(request)

    record(events.SESSION_ENDED, request=request, actor_user_id=actor)

    if id_token:
        target = end_session_url(id_token)
        if target:
            return redirect(target)
    return redirect(settings.LOGOUT_REDIRECT_URL)
