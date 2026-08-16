"""Import a monolith billing artifact. Dry-run by default.

Output is aggregate counts only. This command's output ends up in tickets and
chat, which is no place for a customer list with subscription state attached.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from legacy_migration.artifacts import ArtifactError, load_protected_key
from legacy_migration.importer import DryRunRequired, load, reconcile, run
from legacy_migration.models import ImportRun


class Command(BaseCommand):
    help = 'Import an encrypted monolith billing artifact into Haresign Billing.'

    def add_arguments(self, parser):
        parser.add_argument('artifact', help='Path to the encrypted artifact.')
        parser.add_argument('--key-file', required=True, help='Path to the migration key.')
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply the artifact. Requires a successful dry-run of the same artifact.',
        )

    def handle(self, *args, **options):
        try:
            key = load_protected_key(options['key_file'])
            payload, digest = load(Path(options['artifact']).read_bytes(), key)
        except (ArtifactError, OSError) as exc:
            raise CommandError(str(exc)) from exc

        operation = ImportRun.Operation.APPLY if options['apply'] else ImportRun.Operation.DRY_RUN
        try:
            import_run = run(payload, digest, operation=operation)
        except DryRunRequired as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f'{operation} — {import_run.status}')
        self.stdout.write(f'  artifact  {digest[:16]}…')
        self.stdout.write('  source:')
        for key_name in sorted(import_run.source_counts):
            self.stdout.write(f'    {key_name:24} {import_run.source_counts[key_name]}')
        self.stdout.write('  result:')
        for key_name in sorted(import_run.result_counts):
            self.stdout.write(f'    {key_name:24} {import_run.result_counts[key_name]}')

        if import_run.conflict_counts:
            self.stdout.write(self.style.ERROR('  conflicts (run aborted, nothing written):'))
            for key_name in sorted(import_run.conflict_counts):
                self.stdout.write(f'    {key_name:24} {import_run.conflict_counts[key_name]}')
            raise CommandError('Conflicts found. Resolve them at the source and re-export.')

        if import_run.status != ImportRun.Status.SUCCEEDED:
            raise CommandError(f'Run finished with status {import_run.status}.')

        if options['apply']:
            self.stdout.write('  reconciliation:')
            for key_name, value in sorted(reconcile(payload).items()):
                self.stdout.write(f'    {key_name:24} {value}')
        else:
            self.stdout.write(
                self.style.WARNING('Dry run only. Nothing was written. Re-run with --apply.')
            )
