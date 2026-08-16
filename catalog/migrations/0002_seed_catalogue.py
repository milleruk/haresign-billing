"""Seed the initial catalogue.

A data migration, not a management command, for the same reason Identity's roles
are: the catalogue is not optional. A deployment that has run its migrations and
has no products cannot answer the entitlement API, and "remember to run the seed
command" is not a deployment step anybody remembers at 2am.

Idempotent and matched on the stable `key`, so re-running rewrites names in place
and never orphans a subscription. The reverse is a no-op: unseeding a catalogue
that live subscriptions point at would cascade into deleting them.
"""

from django.db import migrations

from catalog.seed import apply as apply_seed


def forwards(apps, schema_editor):
    apply_seed(apps, schema_editor)


def backwards(apps, schema_editor):
    """Deliberately nothing.

    Products and plans are referenced by subscriptions with `PROTECT`, so a real
    reverse would either fail or take live billing state with it. Retiring a plan
    is `is_active=False`, applied deliberately.
    """


class Migration(migrations.Migration):
    dependencies = [('catalog', '0001_initial')]

    operations = [migrations.RunPython(forwards, backwards)]
