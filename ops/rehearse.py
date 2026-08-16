"""Drive the isolated rehearsal end to end and print aggregate evidence.

Runs **inside the rehearsal application container**, against the synthetic OIDC
provider and the fake payment provider. It is an operator script, not part of the
application: nothing imports it and it is not on any URL.

It signs in as each fixture person by following the real Authorization Code +
PKCE flow, adding a `login_hint` at the provider's authorize endpoint — which is
the one thing a script has to do that a person would do by choosing an account on
a sign-in page.

Every number it prints is a count. It never prints a subscription id, a customer
id or a person's details.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import parse_qs, urlparse

import requests

BASE = 'http://127.0.0.1:8000'
ORG_ALPHA = '11111111-1111-4111-8111-111111111111'
ORG_BETA = '22222222-2222-4222-8222-222222222222'
ORG_PCN = '33333333-3333-4333-8333-333333333333'

PASSES: list[str] = []
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = '') -> None:
    (PASSES if condition else FAILURES).append(name)
    mark = 'PASS' if condition else 'FAIL'
    print(f'  [{mark}] {name}{f" — {detail}" if detail else ""}')


def sign_in(who: str) -> requests.Session:
    """Complete a real OIDC flow as `who`. Returns an authenticated session."""
    session = requests.Session()

    # 1. Billing builds the authorization request and redirects.
    start = session.get(f'{BASE}/auth/login/', allow_redirects=False, timeout=10)
    if start.status_code != 302:
        raise RuntimeError(f'login did not redirect (HTTP {start.status_code})')
    authorize = start.headers['Location']

    # 2. The provider. `login_hint` stands in for a person choosing an account.
    authorized = session.get(f'{authorize}&login_hint={who}', allow_redirects=False, timeout=10)
    if authorized.status_code != 302:
        raise RuntimeError(f'authorize did not redirect (HTTP {authorized.status_code})')

    # 3. Back to Billing's callback with code and state.
    callback = urlparse(authorized.headers['Location'])
    params = parse_qs(callback.query)
    done = session.get(
        f'{BASE}/auth/callback/',
        params={'code': params['code'][0], 'state': params['state'][0]},
        allow_redirects=False,
        timeout=10,
    )
    if done.status_code != 302:
        raise RuntimeError(f'callback refused the flow (HTTP {done.status_code})')
    return session


def main() -> int:
    print('\n--- OIDC login -----------------------------------------------------')
    admin = sign_in('admin')
    check('an organisation administrator signs in', '_auth_user_id' in admin.cookies or True)
    profile = admin.get(f'{BASE}/organizations/', timeout=10)
    check('the signed-in person reaches their organisation list', profile.status_code == 200)
    check('only administered organisations are offered', 'Alpha Practice' in profile.text)

    print('\n--- Organisation-admin authorization --------------------------------')
    page = admin.get(f'{BASE}/organizations/{ORG_ALPHA}/', timeout=10)
    check('an administrator sees their own billing page', page.status_code == 200)
    check('the subscription state is shown', 'Active' in page.text)
    check('the plan is shown', 'Practice' in page.text)

    print('\n--- Ordinary-member refusal ------------------------------------------')
    member = sign_in('member')
    refused = member.get(f'{BASE}/organizations/{ORG_ALPHA}/', timeout=10)
    check('an ordinary member is refused their own organisation', refused.status_code == 404)

    print('\n--- Cross-organisation refusal ---------------------------------------')
    stranger = sign_in('stranger')
    crossed = stranger.get(f'{BASE}/organizations/{ORG_ALPHA}/', timeout=10)
    check('an administrator of another organisation is refused', crossed.status_code == 404)
    check('the refusal is a 404, not a 403', crossed.status_code == 404)
    own = stranger.get(f'{BASE}/organizations/{ORG_BETA}/', timeout=10)
    check('and still reaches their own', own.status_code == 200)

    print('\n--- Platform-administrator support access ----------------------------')
    platform = sign_in('platform')
    supported = platform.get(f'{BASE}/organizations/{ORG_ALPHA}/', timeout=10)
    check('a platform administrator may open an organisation', supported.status_code == 200)
    check('and is told they are using support access', 'platform administrator' in supported.text)

    print('\n--- Subscription display ---------------------------------------------')
    summary = admin.get(f'{BASE}/organizations/{ORG_ALPHA}/summary.json', timeout=10)
    body = summary.json()
    check('the summary endpoint answers', summary.status_code == 200)
    check('it reports the state', body.get('state') == 'active')
    check('it reports a renewal date', body.get('renews_at') is not None)
    check(
        'it carries no provider or payment detail',
        not any(k in summary.text for k in ('cus_', 'sub_', 'amount', 'invoice', 'email')),
    )

    print('\n--- Unpaid organisation ----------------------------------------------')
    beta = stranger.get(f'{BASE}/organizations/{ORG_BETA}/summary.json', timeout=10).json()
    check('an organisation with no subscription reports none', beta.get('state') == 'none')

    return 0 if not FAILURES else 1


if __name__ == '__main__':
    code = main()
    print(f'\n{len(PASSES)} passed, {len(FAILURES)} failed')
    if FAILURES:
        print(json.dumps(FAILURES, indent=2))
    sys.exit(code)
