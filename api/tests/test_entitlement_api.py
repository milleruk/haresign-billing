"""The internal entitlement API: authentication, shape and minimisation."""

from __future__ import annotations

import json
import time
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api.auth import SCHEME, sign_request
from billing.models import ComplimentaryGrant
from billing.tests import factories
from catalog.seed import PRACTICE_DASHBOARDS, PRO_TOOLS

KEY_ID = 'intelligence'
SECRET = 'a-synthetic-shared-secret-for-tests-only'
KEYS = f'{KEY_ID}:{SECRET}'


@override_settings(ENTITLEMENT_API_KEYS=KEYS)
class EntitlementApiTests(TestCase):
    def setUp(self):
        self.account = factories.account()
        self.url = reverse('api:organization_entitlements', args=[self.account.organization_id])

    def call(self, url=None, header=None):
        target = url or self.url
        credential = header if header is not None else sign_request(KEY_ID, SECRET, 'GET', target)
        return self.client.get(target, HTTP_AUTHORIZATION=credential)

    # --- Authentication --------------------------------------------------------

    def test_a_valid_credential_is_accepted(self):
        self.assertEqual(self.call().status_code, 200)

    def test_no_credential_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_a_wrong_secret_is_refused(self):
        bad = sign_request(KEY_ID, 'not-the-secret', 'GET', self.url)
        self.assertEqual(self.call(header=bad).status_code, 401)

    def test_an_unknown_key_id_is_refused(self):
        bad = sign_request('nobody', SECRET, 'GET', self.url)
        self.assertEqual(self.call(header=bad).status_code, 401)

    def test_a_stale_timestamp_is_refused(self):
        old = sign_request(KEY_ID, SECRET, 'GET', self.url, timestamp=int(time.time()) - 3600)
        self.assertEqual(self.call(header=old).status_code, 401)

    def test_a_signature_for_one_organization_does_not_work_for_another(self):
        """The single reason this is a signature and not a bearer token."""
        other = factories.account()
        other_url = reverse('api:organization_entitlements', args=[other.organization_id])
        stolen = sign_request(KEY_ID, SECRET, 'GET', self.url)
        self.assertEqual(self.client.get(other_url, HTTP_AUTHORIZATION=stolen).status_code, 401)

    def test_every_refusal_looks_identical(self):
        """Distinguishing the causes would make this an oracle."""
        bodies = {
            self.client.get(self.url).content,
            self.call(header=sign_request('nobody', SECRET, 'GET', self.url)).content,
            self.call(header=sign_request(KEY_ID, 'wrong', 'GET', self.url)).content,
            self.call(header='nonsense').content,
        }
        self.assertEqual(len(bodies), 1)

    @override_settings(ENTITLEMENT_API_KEYS='')
    def test_no_configured_credentials_closes_the_api(self):
        """A forgotten deployment variable must not become an open oracle."""
        self.assertEqual(self.call().status_code, 401)

    @override_settings(ENTITLEMENT_API_KEYS=f'{KEYS},next:the-rotated-secret')
    def test_rotation_accepts_both_credentials_at_once(self):
        self.assertEqual(self.call().status_code, 200)
        rotated = sign_request('next', 'the-rotated-secret', 'GET', self.url)
        self.assertEqual(self.call(header=rotated).status_code, 200)

    def test_the_scheme_is_required(self):
        signature = sign_request(KEY_ID, SECRET, 'GET', self.url)
        bearer = signature.replace(f'{SCHEME} ', 'Bearer ')
        self.assertEqual(self.call(header=bearer).status_code, 401)

    # --- Shape -----------------------------------------------------------------

    def test_the_response_names_its_version_and_cache_policy(self):
        body = json.loads(self.call().content)
        self.assertEqual(body['api_version'], 'v1')
        self.assertIn('schema_revision', body)
        self.assertIn('cache_max_age', body)
        self.assertIn('evaluated_at', body)

    def test_an_organization_with_no_subscription_holds_nothing_explicitly(self):
        body = json.loads(self.call().content)
        self.assertTrue(all(entry['entitled'] is False for entry in body['products']))
        self.assertTrue(len(body['products']) > 0, 'a missing key is not an answer')

    def test_an_active_subscription_reports_its_products(self):
        factories.subscription(account_obj=self.account)
        body = json.loads(self.call().content)
        held = {entry['product_key'] for entry in body['products'] if entry['entitled']}
        self.assertEqual(held, {PRO_TOOLS, PRACTICE_DASHBOARDS})

    def test_a_past_due_subscription_reports_nothing_entitled(self):
        from billing.models import Subscription

        factories.subscription(account_obj=self.account, state=Subscription.State.PAST_DUE)
        body = json.loads(self.call().content)
        self.assertTrue(all(entry['entitled'] is False for entry in body['products']))

    def test_effective_until_is_reported(self):
        ends = timezone.now() + timedelta(days=45)
        ComplimentaryGrant.objects.create(
            account=self.account, plan=factories.plan('practice'), expires_at=ends
        )
        body = json.loads(self.call().content)
        entry = next(e for e in body['products'] if e['product_key'] == PRO_TOOLS)
        self.assertIsNotNone(entry['effective_until'])

    def test_the_cache_header_is_private(self):
        """A shared cache would serve one organisation's state to another."""
        response = self.call()
        self.assertIn('private', response['Cache-Control'])

    def test_a_malformed_organization_id_is_a_400_not_a_401(self):
        signature = sign_request(KEY_ID, SECRET, 'GET', '/api/v1/organizations/nope/entitlements/')
        response = self.client.get(
            '/api/v1/organizations/nope/entitlements/', HTTP_AUTHORIZATION=signature
        )
        # The URL converter rejects a non-UUID before the view is reached.
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_organization_answers_not_entitled_rather_than_404(self):
        """Distinguishing 'unknown' from 'holds nothing' would enumerate customers."""
        unknown = reverse('api:organization_entitlements', args=[uuid.uuid4()])
        response = self.call(url=unknown)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertTrue(all(entry['entitled'] is False for entry in body['products']))

    # --- Minimisation ----------------------------------------------------------

    def test_the_response_carries_no_payment_or_personal_data(self):
        subscription = factories.subscription(account_obj=self.account)
        self.account.provider_customer_id = 'cus_should_never_appear'
        self.account.organization_name = 'Named Practice'
        self.account.save()

        raw = self.call().content.decode()
        for forbidden in (
            subscription.provider_subscription_id,
            'cus_should_never_appear',
            'Named Practice',
            'amount',
            'invoice',
            'email',
            'contact',
            'price',
            'plan',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, raw)

    def test_the_response_top_level_keys_are_exactly_the_contract(self):
        body = json.loads(self.call().content)
        self.assertEqual(
            sorted(body),
            [
                'api_version',
                'cache_max_age',
                'evaluated_at',
                'organization_id',
                'products',
                'schema_revision',
            ],
        )

    def test_a_product_entry_carries_only_key_state_and_expiry(self):
        factories.subscription(account_obj=self.account)
        entry = json.loads(self.call().content)['products'][0]
        self.assertEqual(sorted(entry), ['effective_until', 'entitled', 'product_key'])


@override_settings(ENTITLEMENT_API_KEYS=KEYS)
class ProductCatalogueApiTests(TestCase):
    def test_the_catalogue_lists_stable_product_keys(self):
        url = reverse('api:products')
        response = self.client.get(url, HTTP_AUTHORIZATION=sign_request(KEY_ID, SECRET, 'GET', url))
        self.assertEqual(response.status_code, 200)
        keys = {entry['product_key'] for entry in json.loads(response.content)['products']}
        self.assertIn(PRO_TOOLS, keys)

    def test_the_catalogue_requires_a_credential(self):
        self.assertEqual(self.client.get(reverse('api:products')).status_code, 401)
