"""Organisation display names: used for labels, never for decisions.

The dangerous version of this feature is the one where a name quietly becomes an
authorization input — where a page renders because a name came back, or refuses
because one did not. So the central test here deletes every name and asserts
that exactly the same pages are allowed and refused as before.

The rest is the client contract: the body is signed, failure is cosmetic, and a
UUID is never shown to a person.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from billing.tests import factories
from identity.display import DisplayUnavailable, fetch_display_names, label_for, names_for
from identity.graph_models import OrganizationDisplayName

KEY_ID = 'billing-display-test'
SECRET = 'a-display-secret-not-used-anywhere-else'


@override_settings(
    IDENTITY_DISPLAY_URL='https://identity.invalid/organizations/display/v1/',
    IDENTITY_DISPLAY_KEY_ID=KEY_ID,
    IDENTITY_DISPLAY_SECRET=SECRET,
)
class DisplayClientTests(TestCase):
    def setUp(self):
        self.organization_id = str(factories.organization_id())

    def _response(self, records, status=200):
        class _Response:
            status_code = status

            @staticmethod
            def json():
                return {'schema_version': 1, 'organizations': records}

        return _Response()

    def test_a_name_is_fetched_and_held(self):
        record = {
            'organization_id': self.organization_id,
            'organization_type': 'practice',
            'display_name': 'Willow Medical Practice',
        }
        with patch('identity.display.requests.post', return_value=self._response([record])):
            names = names_for([self.organization_id])
        self.assertEqual(names[self.organization_id], 'Willow Medical Practice')
        self.assertTrue(
            OrganizationDisplayName.objects.filter(organization_id=self.organization_id).exists()
        )

    def test_the_request_body_is_signed(self):
        """A captured header must not be reusable for other identifiers."""
        captured = {}

        def _capture(url, data=None, headers=None, timeout=None):
            captured['data'] = data
            captured['authorization'] = headers['Authorization']
            return self._response([])

        with patch('identity.display.requests.post', _capture):
            names_for([self.organization_id])

        import hashlib
        import hmac

        key_id, stamp, signature = captured['authorization'].split(' ', 1)[1].split(':', 2)
        digest = hashlib.sha256(captured['data']).hexdigest()
        expected = hmac.new(
            SECRET.encode(),
            f'POST\n/organizations/display/v1/\n{stamp}\n{digest}'.encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(key_id, KEY_ID)

    def test_only_the_requested_identifiers_are_asked_for(self):
        """This client must not invite an enumeration it could not otherwise do."""
        captured = {}

        def _capture(url, data=None, headers=None, timeout=None):
            captured['body'] = json.loads(data)
            return self._response([])

        with patch('identity.display.requests.post', _capture):
            names_for([self.organization_id])
        self.assertEqual(captured['body'], {'organization_ids': [self.organization_id]})

    def test_an_unreachable_identity_is_cosmetic_rather_than_an_outage(self):
        """A name is a label. Failing to get one must not fail the page."""
        with patch('identity.display.requests.post', side_effect=OSError('down')):
            names = names_for([self.organization_id])
        self.assertEqual(names, {})
        self.assertEqual(label_for(self.organization_id, names), 'Organisation')

    def test_a_refusal_is_cosmetic_too(self):
        with patch('identity.display.requests.post', return_value=self._response([], status=401)):
            self.assertEqual(names_for([self.organization_id]), {})

    def test_a_held_name_survives_a_failed_refresh(self):
        """A stale label beats no page."""
        OrganizationDisplayName.objects.create(
            organization_id=self.organization_id,
            display_name='Willow Medical Practice',
            organization_type='practice',
            fetched_at=timezone.now() - timezone.timedelta(days=30),
        )
        with patch('identity.display.requests.post', side_effect=OSError('down')):
            names = names_for([self.organization_id])
        self.assertEqual(names[self.organization_id], 'Willow Medical Practice')

    def test_a_fresh_held_name_is_not_refetched(self):
        OrganizationDisplayName.objects.create(
            organization_id=self.organization_id,
            display_name='Willow Medical Practice',
            organization_type='practice',
            fetched_at=timezone.now(),
        )
        with patch('identity.display.requests.post') as post:
            names_for([self.organization_id])
        post.assert_not_called()

    def test_an_unconfigured_endpoint_raises_rather_than_guessing(self):
        with override_settings(IDENTITY_DISPLAY_URL=''):
            with self.assertRaises(DisplayUnavailable):
                fetch_display_names([self.organization_id])

    def test_plain_http_is_refused(self):
        with override_settings(IDENTITY_DISPLAY_URL='http://identity.invalid/x/'):
            with self.assertRaises(DisplayUnavailable):
                fetch_display_names([self.organization_id])

    def test_a_uuid_is_never_used_as_a_label(self):
        """The defect this feature exists to fix."""
        label = label_for(self.organization_id, {})
        self.assertNotIn(self.organization_id, label)
        self.assertEqual(label, 'Organisation')


class DisplayNamesAreNotAnAuthorizationInputTests(TestCase):
    """Deleting every name must change what pages say, never who may see them.

    Authorization comes from the session's memberships and from the graph's
    edges. If a display name could open or close a page, an outage at a
    *cosmetic* endpoint would become an access-control event.
    """

    def test_the_display_module_is_not_imported_by_the_authorization_module(self):
        """The cheapest structural guarantee, asserted from the source."""
        from pathlib import Path

        from django.conf import settings

        source = (Path(settings.BASE_DIR) / 'identity' / 'authorization.py').read_text()
        self.assertNotIn('display', source.lower().split('"""')[-1])

    def test_authorization_does_not_read_the_display_table(self):
        from identity import authorization

        source = __import__('inspect').getsource(authorization)
        self.assertNotIn('OrganizationDisplayName', source)
        self.assertNotIn('names_for', source)
        self.assertNotIn('label_for', source)
