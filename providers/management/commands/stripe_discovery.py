"""Read the provider's catalogue and aggregate state. Reads only, never writes.

The single audited command permitted to reach Stripe before cutover — AGENTS.md,
"Phase 4B exception", item 4. The expected mode is a required argument and is
checked against the credential's own prefix and the objects' `livemode` flags, so
running it against the wrong account is a refusal rather than a report nobody
notices is about the wrong Stripe.

    manage.py stripe_discovery --expect-mode test
    manage.py stripe_discovery --expect-mode live --show-catalogue

`--show-catalogue` prints price and product ids, which is what the operator needs
in order to write a mapping for `map_provider_prices`. Customer and subscription
identifiers are never printed at any verbosity.
"""

from django.core.management.base import BaseCommand, CommandError

from providers.discovery import DiscoveryRefused, discover


class Command(BaseCommand):
    help = 'Read-only discovery of the payment provider catalogue and aggregate state.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--expect-mode',
            required=True,
            choices=['live', 'test'],
            help='Which Stripe mode this run is expected to read. Checked, not assumed.',
        )
        parser.add_argument(
            '--show-catalogue',
            action='store_true',
            help='Print product and price identifiers, for writing a price mapping.',
        )

    def handle(self, *args, **options):
        try:
            report = discover(
                expect_mode=options['expect_mode'],
                include_catalogue=options['show_catalogue'],
            )
        except DiscoveryRefused as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(f'Provider {report.provider} — {report.observed_mode} mode')
        )
        for key, value in report.counts.items():
            if key in ('provider', 'mode'):
                continue
            self.stdout.write(f'  {key:42} {value}')

        if report.catalogue:
            self.stdout.write('')
            self.stdout.write('Catalogue (product · price · amount · recurrence · state)')
            for price in report.catalogue:
                amount = (
                    f'{price.unit_amount_minor / 100:.2f} {price.currency}'
                    if price.unit_amount_minor is not None
                    else 'variable'
                )
                recurrence = (
                    f'every {price.interval_count} {price.interval}'
                    if price.is_recurring
                    else 'one-off'
                )
                state = 'active' if price.active and price.product_active else 'archived'
                self.stdout.write(
                    f'  {price.product_name or price.product_id} · {price.price_id} · '
                    f'{amount} · {recurrence} · {state}'
                )
