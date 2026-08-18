"""Writing verified provider price references into the catalogue.

Every `PlanPrice.provider_price_id` is blank until this runs, which is why
nothing is purchasable and why no webhook naming a price can resolve to a plan.
Filling them in is the one catalogue mutation the Stripe cutover requires, and it
is the mutation most worth being paranoid about: a price id written against the
wrong plan sells the wrong thing at the wrong price, and the mistake is only
visible once somebody has been charged.

So the rules here are deliberately unhelpful to anyone in a hurry.

**Mapping is by id, stated by a human.** Never by product name, plan name or
price nickname. Display names are edited in the Stripe dashboard by whoever
happens to be there; matching on one means a rename silently re-points a plan.

**Every stated mapping is verified against the provider** before it is written:
the price must exist, be active, belong to an active product, be recurring, and
agree with the catalogue row on interval, interval count, currency and amount. A
disagreement is a refusal, never a correction — if Stripe says £49 and the
catalogue says £10, one of them is wrong and this code does not get to decide
which.

**Nothing is applied unless everything verifies.** A partial application leaves
some plans purchasable and others not, which looks like a working launch and is
not one.

**An existing reference is never silently replaced.** Re-pointing a plan at a new
price is a real commercial act — it changes what a new subscriber is charged —
and it needs `force` and lands in the audit trail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from audit import events as audit_events
from audit.services import record
from providers.discovery import LIVE, TEST, discovery_provider

from .models import PlanPrice

logger = logging.getLogger('haresign.billing')

# Outcomes. `mapped` and `unchanged` are the only two that are not a stop.
MAPPED = 'mapped'
UNCHANGED = 'unchanged'
REFUSED = 'refused'


class MappingRefused(RuntimeError):
    """The mapping did not verify. Nothing was written.

    Carries every outcome, not only the failing ones, so a caller can print the
    whole decision table. An operator who is told "3 of 4 refused" and not which
    three will guess, and guessing is what this module exists to prevent.
    """

    def __init__(self, message: str, outcomes: list | None = None):
        super().__init__(message)
        self.outcomes = outcomes or []


@dataclass(frozen=True)
class MappingEntry:
    """One stated mapping: this plan, at this interval, is this provider price."""

    plan_key: str
    interval: str
    price_id: str

    @classmethod
    def parse(cls, text: str) -> MappingEntry:
        """`plan_key:interval=price_id`. Strict, because a typo here sells a plan."""
        left, _, price_id = text.partition('=')
        plan_key, _, interval = left.partition(':')
        plan_key, interval, price_id = plan_key.strip(), interval.strip(), price_id.strip()
        if not plan_key or not interval or not price_id:
            raise MappingRefused(
                f'Malformed mapping {text!r}. Expected plan_key:interval=price_id.'
            )
        return cls(plan_key=plan_key, interval=interval, price_id=price_id)


@dataclass(frozen=True)
class MappingOutcome:
    """What was decided for one stated mapping, and why."""

    plan_key: str
    interval: str
    outcome: str
    reason: str = ''
    # The price id is echoed so an operator can check what they asked for. It is a
    # catalogue identifier, not a customer one.
    price_id: str = ''

    @property
    def is_stop(self) -> bool:
        return self.outcome == REFUSED


def map_prices(
    entries: list[MappingEntry],
    *,
    expect_mode: str,
    apply: bool = False,
    force: bool = False,
    request=None,
) -> list[MappingOutcome]:
    """Verify every stated mapping, and write them all or write none.

    Returns one outcome per entry. Raises `MappingRefused` **after** building the
    outcomes if any of them is a stop, so the caller can report exactly what
    failed rather than only the first failure.
    """
    if expect_mode not in (LIVE, TEST):
        raise MappingRefused("Mapping needs an explicit expected mode: 'live' or 'test'.")
    if not entries:
        raise MappingRefused('No mappings were given.')

    provider = discovery_provider()
    catalogue = {price.price_id: price for price in provider.list_catalogue()}

    # A provider price maps to exactly one plan price — the catalogue enforces it
    # with a unique constraint, and stating it twice in one request is a mistake
    # worth naming before the constraint does.
    stated = [entry.price_id for entry in entries]
    duplicated = {price_id for price_id in stated if stated.count(price_id) > 1}

    outcomes: list[MappingOutcome] = []
    writes: list[tuple[PlanPrice, str]] = []

    for entry in entries:
        outcome, row = _verify(
            entry,
            catalogue=catalogue,
            duplicated=duplicated,
            expect_mode=expect_mode,
            force=force,
        )
        outcomes.append(outcome)
        if outcome.outcome == MAPPED and row is not None:
            writes.append((row, entry.price_id))

    stops = [outcome for outcome in outcomes if outcome.is_stop]
    if stops:
        for stop in stops:
            record(
                audit_events.PROVIDER_PRICE_MAPPING_REFUSED,
                request=request,
                metadata={
                    'plan_key': stop.plan_key,
                    'interval': stop.interval,
                    'reason': stop.reason,
                    'mode': expect_mode,
                },
            )
        raise MappingRefused(
            f'{len(stops)} of {len(entries)} mappings did not verify. Nothing was written.',
            outcomes,
        )

    if not apply:
        return outcomes

    with transaction.atomic():
        for row, price_id in writes:
            previous = row.provider_price_id
            row.provider_price_id = price_id
            row.save(update_fields=['provider_price_id', 'updated_at'])
            record(
                audit_events.PROVIDER_PRICE_MAPPED,
                request=request,
                metadata={
                    'plan_key': row.plan.key,
                    'interval': row.interval,
                    'mode': expect_mode,
                    'replaced_existing': bool(previous),
                    # The amount is a catalogue fact, not a person's payment.
                    'amount_minor': row.amount_minor,
                    'currency': row.currency,
                },
            )
    logger.info('catalogue: mapped %s provider prices in %s mode', len(writes), expect_mode)
    return outcomes


def _verify(entry, *, catalogue, duplicated, expect_mode, force):
    """One entry against the provider and the catalogue. Returns (outcome, row)."""

    def refuse(reason: str) -> tuple[MappingOutcome, None]:
        return (
            MappingOutcome(
                plan_key=entry.plan_key,
                interval=entry.interval,
                outcome=REFUSED,
                reason=reason,
                price_id=entry.price_id,
            ),
            None,
        )

    if entry.price_id in duplicated:
        return refuse('price_id_stated_more_than_once')

    row = (
        PlanPrice.objects.select_related('plan')
        .filter(plan__key=entry.plan_key, interval=entry.interval, provider='stripe')
        .first()
    )
    if row is None:
        return refuse('no_such_plan_price')

    remote = catalogue.get(entry.price_id)
    if remote is None:
        return refuse('price_not_found_at_provider')

    expected_livemode = expect_mode == LIVE
    if remote.livemode != expected_livemode:
        return refuse('price_mode_mismatch')
    if not remote.active:
        return refuse('price_archived_at_provider')
    if not remote.product_active:
        return refuse('product_archived_at_provider')
    if not remote.is_recurring:
        return refuse('price_is_not_recurring')
    if remote.interval != row.interval:
        return refuse('interval_mismatch')
    if remote.interval_count != 1:
        # A price billed every three months is a different commercial product from
        # the monthly one this row describes, and the catalogue has nowhere to say so.
        return refuse('interval_count_not_supported')
    if (remote.currency or '').upper() != (row.currency or '').upper():
        return refuse('currency_mismatch')
    if remote.unit_amount_minor != row.amount_minor:
        return refuse('amount_mismatch')

    clash = (
        PlanPrice.objects.filter(provider='stripe', provider_price_id=entry.price_id)
        .exclude(pk=row.pk)
        .first()
    )
    if clash is not None:
        return refuse('price_already_mapped_to_another_plan')

    if row.provider_price_id == entry.price_id:
        return (
            MappingOutcome(
                plan_key=entry.plan_key,
                interval=entry.interval,
                outcome=UNCHANGED,
                price_id=entry.price_id,
            ),
            None,
        )

    if row.provider_price_id and not force:
        return refuse('already_mapped_to_a_different_price')

    return (
        MappingOutcome(
            plan_key=entry.plan_key,
            interval=entry.interval,
            outcome=MAPPED,
            reason='replaces_existing' if row.provider_price_id else '',
            price_id=entry.price_id,
        ),
        row,
    )
