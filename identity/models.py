"""Local shells for authenticated Identity users, and their session state.

There is no password, no email verification, no registration and no profile in
this service. Identity owns people. What lives here is the minimum needed to hang
a Django session on: the permanent Identity user UUID, a display name for the
header, and the memberships captured at sign-in.

`IdentityUser` subclasses `AbstractBaseUser` only so `django.contrib.auth`,
`request.user` and the admin work. Every row is created with an unusable password
and `set_password` is refused outright — a credential store is precisely the thing
this service must not accumulate.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone


class IdentityUserManager(models.Manager):
    def create_user(self, identity_user_id, **extra):
        user = self.model(identity_user_id=identity_user_id, **extra)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, identity_user_id, password=None, **extra):
        """Break-glass local operator for the Django admin.

        Deliberately still password-less: an operator reaches the admin through
        the same OIDC sign-in as everybody else, and this exists so
        `createsuperuser` in a rehearsal stack does not fail.
        """
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        return self.create_user(identity_user_id, **extra)


class IdentityUser(AbstractBaseUser, PermissionsMixin):
    """A person, as far as this service needs to know them."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The permanent Identity user UUID — the OIDC `sub`. Unique, never reissued,
    # and the only durable fact about a person this service stores.
    identity_user_id = models.UUIDField(unique=True, db_index=True)

    # Display copies from the ID token, refreshed on each sign-in. Not
    # authoritative; nothing branches on them.
    display_name = models.CharField(max_length=200, blank=True, default='')
    email = models.EmailField(blank=True, default='')

    # There is deliberately **no** platform-administrator flag here. Phase 4A had
    # one, mirrored from a `haresign_platform_admin` claim, and it authorised a
    # support bypass into any organisation's billing. Two things were wrong with
    # it: Identity does not emit that claim at all, so the flag was never set
    # outside the synthetic rehearsal; and the bypass itself is a role granting
    # billing access, which is the shape the ownership contract exists to forbid.
    # A platform administrator who needs an organisation's billing gets there by
    # being an administrator of that organisation.

    is_active = models.BooleanField(default=True)
    # Django admin access, and deliberately not set from any claim: reaching the
    # Django admin of the billing service is a local operational decision, not an
    # Identity role. It grants nothing in the billing pages either — those are
    # gated on organisation-administrator membership and on nothing else.
    is_staff = models.BooleanField(default=False)

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = 'identity_user_id'
    REQUIRED_FIELDS = []

    objects = IdentityUserManager()

    class Meta:
        ordering = ['display_name', 'identity_user_id']
        verbose_name = 'Identity user'

    def __str__(self) -> str:
        return self.display_name or str(self.identity_user_id)

    def set_password(self, raw_password):
        """Refuse to hold a password, always.

        Django calls this internally with `None` to mark a password unusable,
        which is the only accepted use. Anything else is a caller trying to make
        this service a credential store.
        """
        if raw_password is not None:
            raise RuntimeError(
                'Haresign Billing does not store passwords. Authentication is OIDC '
                'against Haresign Identity.'
            )
        return super().set_password(None)

    def get_username(self) -> str:
        return str(self.identity_user_id)


class SessionMembership(models.Model):
    """An organisation membership as Identity reported it at sign-in.

    Stored per session, not per user, and deleted when the session ends. That is
    the distinction that keeps this from becoming a copy of Identity's membership
    table: it is a *claim about this login*, with a timestamp, and it expires.

    `captured_at` is what makes stale membership visible. A membership older than
    `IDENTITY_MEMBERSHIP_MAX_AGE` is not silently trusted — the request is sent
    back through Identity to re-authorize. See docs/identity-integration.md.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_key = models.CharField(max_length=40, db_index=True)
    user = models.ForeignKey(
        'identity.IdentityUser', on_delete=models.CASCADE, related_name='session_memberships'
    )

    organization_id = models.UUIDField(db_index=True)
    organization_name = models.CharField(max_length=255, blank=True, default='')
    organization_type = models.CharField(max_length=20, blank=True, default='')
    # The role key exactly as Identity named it. Compared against
    # `identity.authorization.ADMIN_ROLE_KEYS`, never parsed or inferred.
    role = models.CharField(max_length=64, blank=True, default='')
    is_administrator = models.BooleanField(default=False)

    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['organization_name']
        constraints = [
            models.UniqueConstraint(
                fields=['session_key', 'organization_id'],
                name='identity_session_membership_unique',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.user} @ {self.organization_id} ({self.role})'


# The organisation-graph projection lives in its own module for length, and is
# re-exported here so that `identity.models` remains the one place to look for
# this app's tables.
from .graph_models import (  # noqa: E402,F401  (re-export, deliberately at the end)
    GraphOrganization,
    GraphRelationship,
    OrganizationGraph,
)
