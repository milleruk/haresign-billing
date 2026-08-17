"""What these pages are allowed to show a person.

Two defects prompted this suite, and both were invisible to every existing test
because both rendered *correct* pages. A page that says
`3f3e088b-e5c9-41ee-a706-c8ddd6fa1eca` where a practice name belongs is right
about the organisation and useless to the reader. A page that says `pro_tools`
where a product name belongs is showing the reader an internal contract with
another service.

So these assert the absence of the internal form rather than the presence of the
friendly one: a UUID is never a label, and a stable product key is never a
product name.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from identity.graph_models import OrganizationDisplayName

from . import factories


def _display(organization_id, name, organization_type='practice'):
    OrganizationDisplayName.objects.create(
        organization_id=str(organization_id),
        display_name=name,
        organization_type=organization_type,
        fetched_at=timezone.now(),
    )


class OrganizationNamingTests(TestCase):
    def setUp(self):
        self.account = factories.account(name='')
        self.user = factories.identity_user()
        self.url = reverse('billing:organization', args=[self.account.organization_id])

    def _sign_in(self):
        return factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': factories.ADMIN_ROLE}],
        )

    def test_the_identity_display_name_is_used_as_the_heading(self):
        _display(self.account.organization_id, 'Willow Medical Practice')
        self._sign_in()
        self.assertContains(self.client.get(self.url), 'Willow Medical Practice')

    def test_a_uuid_is_never_rendered_as_a_label(self):
        """The reported defect. The UUID stays internal — it is still the key in
        the page's own URL, and it is not a thing to read."""
        self._sign_in()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        heading = body.split('hs-page-header__title">', 1)[1].split('</h1>', 1)[0]
        self.assertNotIn(str(self.account.organization_id), heading)

    def test_the_organisation_picker_names_rather_than_numbers(self):
        _display(self.account.organization_id, 'Willow Medical Practice')
        self._sign_in()
        response = self.client.get(reverse('billing:home'))
        self.assertContains(response, 'Willow Medical Practice')
        listing = response.content.decode().split('hs-grid', 1)[1]
        self.assertNotIn(f'>{self.account.organization_id}<', listing)

    def test_a_missing_name_falls_back_to_a_label_not_an_identifier(self):
        """Identity being unreachable is cosmetic, and the fallback is honest."""
        self._sign_in()
        response = self.client.get(self.url)
        self.assertContains(response, 'This organisation')
        self.assertNotContains(response, f'>{self.account.organization_id}<')


class ProductNamingTests(TestCase):
    def setUp(self):
        self.account = factories.account(name='Willow Practice')
        self.user = factories.identity_user()
        self.url = reverse('billing:organization', args=[self.account.organization_id])
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': factories.ADMIN_ROLE}],
        )

    def test_products_are_named_from_the_catalogue(self):
        response = self.client.get(self.url)
        for name in ('Premium tools', 'Practice dashboards', 'PCN dashboards'):
            with self.subTest(name=name):
                self.assertContains(response, name)

    def test_no_internal_product_key_is_shown(self):
        """`pro_tools` is the contract with Intelligence, not a product name."""
        response = self.client.get(self.url)
        body = response.content.decode()
        for key in ('pro_tools', 'practice_dashboards', 'pcn_dashboards'):
            with self.subTest(key=key):
                self.assertNotIn(key, body)

    def test_each_product_carries_its_description(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Full PCN-level benchmarking dashboards.')

    def test_an_entitlement_for_an_uncatalogued_product_still_shows_no_key(self):
        """Defensive, and reachable: a catalogue row can be retired while an
        entitlement derived earlier in the same request still names it. The page
        must degrade to something readable rather than leaking the key."""
        from billing.entitlements import OrganizationEntitlements, ProductEntitlement

        derived = OrganizationEntitlements(
            organization_id=str(self.account.organization_id),
            products={
                'retired_thing': ProductEntitlement(product_key='retired_thing', entitled=False)
            },
            evaluated_at=timezone.now(),
        )
        with patch('billing.views.entitlements_for_organization', return_value=derived):
            body = self.client.get(self.url).content.decode()
        self.assertNotIn('retired_thing', body)
        self.assertIn('Retired thing', body)
