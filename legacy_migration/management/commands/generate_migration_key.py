"""Create a migration key file at mode 600.

Refuses to replace an existing file. Losing a key makes an artifact permanently
unreadable, which is the correct failure — but overwriting one silently while an
artifact encrypted under it still exists is not a failure anybody notices until
they need to read it.
"""

from django.core.management.base import BaseCommand, CommandError

from legacy_migration.artifacts import ArtifactError, generate_key_file


class Command(BaseCommand):
    help = 'Generate a 32-byte migration key file at mode 600.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Where to write the key. Never inside the repository.')

    def handle(self, *args, **options):
        try:
            generate_key_file(options['path'])
        except ArtifactError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f'Wrote a migration key to {options["path"]} (mode 600).')
        )
        self.stdout.write(
            'Keep it outside git and away from the artifact. An artifact and its key '
            'in one place is one compromise, not two.'
        )
