"""Fetch the organisation-graph projection from Haresign Identity.

The scheduled entry point. Run it on a timer at `IDENTITY_GRAPH_REFRESH_INTERVAL`,
comfortably inside `IDENTITY_GRAPH_MAX_AGE`, so that one failed refresh does not
immediately close sponsored entitlements — several consecutive ones should.

Output is aggregate only: a version, counts, and how many edges moved. It never
names an organisation, because a scheduled command's output ends up in logs and
the estate's structure is not a thing to accumulate there.

Exit codes matter, because a scheduler reads them:

* **0** — fetched, or already holding a fresh projection.
* **1** — could not refresh. The held projection may still be fresh; the command
  says which, so an alert can distinguish "transient" from "sponsored
  entitlements are about to close".
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from audit import events
from audit.services import record
from identity.graph import GraphError, current_graph, refresh


class Command(BaseCommand):
    help = 'Fetch the organisation-graph projection from Haresign Identity.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help=(
                'Fetch even when the held projection is still fresh. Bypasses the '
                'freshness shortcut only — never validation.'
            ),
        )

    def handle(self, *args, **options):
        try:
            result = refresh(force=options['force'])
        except GraphError as exc:
            held = current_graph()
            record(
                events.GRAPH_REFRESH_FAILED,
                metadata={
                    # The exception type, not its message: a message can quote a
                    # URL or a response body.
                    'error': type(exc).__name__,
                    'holding_version': held.graph_version if held else '',
                    'holding_is_fresh': bool(held and held.is_fresh),
                },
            )
            self.stderr.write(
                json.dumps(
                    {
                        'status': 'failed',
                        'error': type(exc).__name__,
                        'holding_a_projection': held is not None,
                        # The line an alert should key on. False means sponsored
                        # entitlements and new sponsored purchases are closing.
                        'holding_is_fresh': bool(held and held.is_fresh),
                    }
                )
            )
            return

        graph = result.graph
        self.stdout.write(
            json.dumps(
                {
                    'status': 'unchanged' if result.unchanged_version else 'ok',
                    'graph_version': graph.graph_version if graph else '',
                    'source': graph.source if graph else '',
                    'age_seconds': int(graph.age_seconds) if graph else None,
                    'is_fresh': graph.is_fresh if graph else False,
                    'organizations': graph.organization_count if graph else 0,
                    'relationships': graph.relationship_count if graph else 0,
                    # Counts only. Which relationships moved is on the audit row
                    # and the operational alerts, not in a scheduler's log.
                    'relationships_added': result.relationships_added,
                    'relationships_removed': result.relationships_removed,
                }
            )
        )
