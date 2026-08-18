"""Write verified provider price references into the catalogue.

Report-only unless `--apply`, and it verifies every stated mapping against the
provider before writing any of them — see `catalog/price_mapping.py` for what is
checked and why nothing is applied unless everything verifies.

    manage.py map_provider_prices --expect-mode test \\
        --map practice:month=price_... --map practice:year=price_...
    manage.py map_provider_prices --expect-mode live --file /path/to/mapping --apply

The file form is one `plan_key:interval=price_id` per line, `#` for comments. It
exists because a live mapping is written down, reviewed and then run — not typed
at a prompt with real prices in the shell history.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog.price_mapping import MappingEntry, MappingRefused, map_prices


class Command(BaseCommand):
    help = 'Map catalogue plan prices to verified provider price identifiers.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--expect-mode',
            required=True,
            choices=['live', 'test'],
            help='Which Stripe mode the stated prices must belong to.',
        )
        parser.add_argument(
            '--map',
            action='append',
            default=[],
            metavar='PLAN:INTERVAL=PRICE_ID',
            help='One mapping. Repeatable.',
        )
        parser.add_argument(
            '--file',
            default='',
            help='A file of mappings, one per line. Comments start with #.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Write the mappings. Without this the run only verifies and reports.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Allow replacing a plan price that already names a different provider price.',
        )

    def handle(self, *args, **options):
        statements = list(options['map'])
        if options['file']:
            path = Path(options['file'])
            if not path.is_file():
                raise CommandError(f'No mapping file at {path}.')
            statements += [
                line.strip()
                for line in path.read_text().splitlines()
                if line.strip() and not line.strip().startswith('#')
            ]

        try:
            entries = [MappingEntry.parse(statement) for statement in statements]
            outcomes = map_prices(
                entries,
                expect_mode=options['expect_mode'],
                apply=options['apply'],
                force=options['force'],
            )
        except MappingRefused as exc:
            # Print what was decided before failing, so the operator sees every
            # refusal rather than only the count.
            for outcome in getattr(exc, 'outcomes', []):
                detail = f' ({outcome.reason})' if outcome.reason else ''
                self.stdout.write(
                    f'  {outcome.plan_key}:{outcome.interval:6} {outcome.outcome}{detail}'
                )
            raise CommandError(str(exc)) from exc

        for outcome in outcomes:
            style = self.style.SUCCESS if outcome.outcome != 'refused' else self.style.ERROR
            detail = f' ({outcome.reason})' if outcome.reason else ''
            self.stdout.write(
                style(f'  {outcome.plan_key}:{outcome.interval:6} {outcome.outcome}{detail}')
            )

        if not options['apply']:
            self.stdout.write(
                self.style.WARNING('Verified only. Re-run with --apply to write the mappings.')
            )
