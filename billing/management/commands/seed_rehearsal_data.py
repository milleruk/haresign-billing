"""Seed the isolated rehearsal with synthetic billing state.

Refuses to run outside a rehearsal. Everything it writes is invented: the
organisation UUIDs match the synthetic OIDC provider's fixture people, the
provider identifiers are obviously fake, and no real customer is represented.

This is not `seed_preview_data`. There is no Billing preview: a preview of a
billing service is a page showing somebody a subscription state, and a *plausible
but wrong* subscription state is worse than no page at all. This command exists
only for the disposable rehearsal.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from billing.models import BillingAccount, MemberOrganizationLink, Subscription
from billing.services import apply_subscription_snapshot, set_billing_contact
from catalog.models import Plan, PlanPrice

# Fixed, and identical to ops/synthetic_oidc_provider.py. The two files must
# agree or the rehearsal proves nothing.
ORG_ALPHA = '11111111-1111-4111-8111-111111111111'
ORG_BETA = '22222222-2222-4222-8222-222222222222'
ORG_PCN = '33333333-3333-4333-8333-333333333333'


class Command(BaseCommand):
    help = 'Seed synthetic billing state for the isolated rehearsal. Never for production.'

    def handle(self, *args, **options):
        # Two independent guards. A rehearsal marker *and* the fake provider: a
        # command that writes subscriptions must not be one misplaced environment
        # variable away from running against a real stack.
        if not settings.ENVIRONMENT_LABEL:
            raise CommandError(
                'ENVIRONMENT_LABEL is empty, which marks this as production. '
                'This command only runs in a labelled rehearsal environment.'
            )
        if settings.PROVIDER_BACKEND != 'fake':
            raise CommandError('This command only runs against the fake provider.')

        practice_month = PlanPrice.objects.get(plan__key='practice', interval='month')
        pcn_month = PlanPrice.objects.get(plan__key='pcn', interval='month')
        # Give the synthetic prices provider references, so the webhook path can
        # resolve them. Obviously fake values.
        for price, reference in (
            (practice_month, 'price_rehearsal_practice_month'),
            (pcn_month, 'price_rehearsal_pcn_month'),
        ):
            if not price.provider_price_id:
                price.provider_price_id = reference
                price.save(update_fields=['provider_price_id'])

        alpha = self._account(ORG_ALPHA, 'Alpha Practice', 'practice', 'cus_rehearsal_alpha')
        beta = self._account(ORG_BETA, 'Beta Practice', 'practice', 'cus_rehearsal_beta')
        pcn = self._account(ORG_PCN, 'Rehearsal PCN', 'pcn', 'cus_rehearsal_pcn')

        # Alpha pays for itself. Beta pays nothing — its entitlements must be
        # empty, which is half of what the rehearsal proves.
        apply_subscription_snapshot(
            account=alpha,
            provider='fake',
            provider_subscription_id='sub_rehearsal_alpha',
            state=Subscription.State.ACTIVE,
            plan=Plan.objects.get(key='practice'),
            prices=[(practice_month, 1)],
            provider_customer_id='cus_rehearsal_alpha',
            current_period_start=timezone.now() - timedelta(days=1),
            current_period_end=timezone.now() + timedelta(days=29),
            sequence=1,
        )

        # The PCN pays, and its plan covers members — but only for organisations
        # actually linked to it. Beta is deliberately *not* linked.
        apply_subscription_snapshot(
            account=pcn,
            provider='fake',
            provider_subscription_id='sub_rehearsal_pcn',
            state=Subscription.State.ACTIVE,
            plan=Plan.objects.get(key='pcn'),
            prices=[(pcn_month, 1)],
            provider_customer_id='cus_rehearsal_pcn',
            current_period_end=timezone.now() + timedelta(days=200),
            sequence=1,
        )

        set_billing_contact(
            alpha,
            identity_user_id='44444444-4444-4444-8444-444444444444',
            display_name='Alex Administrator',
        )

        self.stdout.write(self.style.SUCCESS('Seeded synthetic rehearsal billing state.'))
        self.stdout.write(f'  billing accounts   {BillingAccount.objects.count()}')
        self.stdout.write(f'  subscriptions      {Subscription.objects.count()}')
        self.stdout.write(f'  member links       {MemberOrganizationLink.objects.count()}')
        del beta

    def _account(self, organization_id, name, organization_type, customer_id):
        account, _ = BillingAccount.objects.get_or_create(
            organization_id=organization_id,
            defaults={
                'organization_name': name,
                'organization_type': organization_type,
                'provider': 'fake',
                'provider_customer_id': customer_id,
            },
        )
        return account
