"""The pre-cutover reconciliation: provider against Billing against Identity.

Read-only, always — there is no apply flag, by design. **Exits non-zero when it
finds a conflict**, so that a cutover script cannot walk past one.

    manage.py cutover_reconciliation
    manage.py cutover_reconciliation --exception cus_... --exception sub_...

A declared exception is a provider customer or subscription an operator has
already examined and knowingly accepted as unmatched. It is counted separately
rather than removed from the totals, so accepting one leaves a mark.
"""

import sys

from django.core.management.base import BaseCommand

from providers.cutover import reconcile_for_cutover


class Command(BaseCommand):
    help = 'Reconcile the provider, Billing and Identity before cutover. Read-only.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--exception',
            action='append',
            default=[],
            help='A provider customer or subscription id knowingly accepted as unmatched.',
        )

    def handle(self, *args, **options):
        report = reconcile_for_cutover(exceptions=set(options['exception']))

        if report.provider != 'stripe':
            # "No conflicts" against a provider holding nothing is the most
            # reassuring output this command can produce and the least
            # meaningful, so it is labelled before the numbers rather than after.
            self.stdout.write(
                self.style.WARNING(
                    f'Provider is {report.provider!r}, not Stripe. The provider-side counts '
                    'below describe nothing at Stripe and prove nothing about a cutover.'
                )
            )

        for key, value in report.counts.items():
            self.stdout.write(f'  {key:42} {value}')

        if report.blocks_cutover:
            self.stdout.write('')
            self.stdout.write(
                self.style.ERROR(
                    f'{report.conflicts} conflict(s). Cutover must stop until each is resolved.'
                )
            )
            sys.exit(1)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('No conflicts.'))
        if report.identity_projection != 'fresh':
            # Not a conflict, and not nothing: a stale projection means sponsored
            # entitlements are failing closed and Identity coverage is unproven.
            self.stdout.write(
                self.style.WARNING(
                    f'Identity organisation projection is {report.identity_projection}. '
                    'Refresh it before relying on the Identity mapping counts.'
                )
            )
