"""Compare local subscription state against the provider's.

Report-only unless `--apply` is given. Output is aggregate counts: this command
is run by operators and its output ends up in tickets and chat, which is no place
for a customer list with financial state attached.
"""

from django.core.management.base import BaseCommand

from providers.reconciliation import reconcile


class Command(BaseCommand):
    help = 'Reconcile local subscriptions against the configured provider.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Write corrections. Without this the run only reports drift.',
        )

    def handle(self, *args, **options):
        run = reconcile(apply=options['apply'])
        self.stdout.write(f'Reconciliation {run.id} — {run.status}')
        for key in sorted(run.counts):
            self.stdout.write(f'  {key:22} {run.counts[key]}')
        if run.status == run.Status.DRIFTED and not options['apply']:
            self.stdout.write(self.style.WARNING('Drift found. Re-run with --apply to correct it.'))
