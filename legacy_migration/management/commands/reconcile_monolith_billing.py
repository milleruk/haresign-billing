"""Compare an artifact against what was imported. Read-only, aggregate counts."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from legacy_migration.artifacts import ArtifactError, load_protected_key
from legacy_migration.importer import load, reconcile


class Command(BaseCommand):
    help = 'Reconcile an imported monolith billing artifact against Billing state.'

    def add_arguments(self, parser):
        parser.add_argument('artifact')
        parser.add_argument('--key-file', required=True)

    def handle(self, *args, **options):
        try:
            key = load_protected_key(options['key_file'])
            payload, digest = load(Path(options['artifact']).read_bytes(), key)
        except (ArtifactError, OSError) as exc:
            raise CommandError(str(exc)) from exc

        counts = reconcile(payload)
        self.stdout.write(f'Reconciliation for artifact {digest[:16]}…')
        for name in sorted(counts):
            self.stdout.write(f'  {name:24} {counts[name]}')

        if counts['state_mismatches'] or counts['missing_locally']:
            self.stdout.write(self.style.ERROR('Reconciliation did not match.'))
        else:
            self.stdout.write(self.style.SUCCESS('Reconciliation matched exactly.'))
