"""Resolve allowlisted file secrets, drop privileges, then run the application.

The allowlist is exhaustive on purpose. A loop over "anything ending in _FILE"
would happily read whatever path an attacker who could set one environment
variable chose to name.
"""

import os
import sys

SECRET_NAMES = (
    'SECRET_KEY',
    'POSTGRES_PASSWORD',
    # Relying-party secret for this service's OIDC client at Haresign Identity.
    'OIDC_CLIENT_SECRET',
    # Shared material for the internal entitlement API's service credentials.
    'ENTITLEMENT_API_KEYS',
    # This service's half of the organisation-graph credential at Haresign
    # Identity. Named here for the same reason as the rest: the file is read
    # while this process is still root, and a moment later it is uid 10001 and a
    # mode-600 root-owned secret is unreadable. Omitting a name does not fall
    # back to the direct form — `env_secret` raises and the application will not
    # boot, on the deployment only.
    'IDENTITY_GRAPH_SECRET',
    # Provider material. Unset in every environment Phase 4A ships; named here so
    # enabling it later is a deployment step, not a code change.
    'STRIPE_SECRET_KEY',
    'STRIPE_WEBHOOK_SECRET',
)
BILLING_UID = 10001
BILLING_GID = 10001


def main():
    for name in SECRET_NAMES:
        file_name = os.environ.get(f'{name}_FILE', '').strip()
        if not file_name:
            continue
        if os.environ.get(name):
            raise RuntimeError(f'{name} and {name}_FILE cannot both be set.')
        with open(file_name) as secret_file:
            os.environ[name] = secret_file.read().strip()
        os.environ.pop(f'{name}_FILE', None)

    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(BILLING_GID)
        os.setuid(BILLING_UID)
    os.environ['HOME'] = '/home/billing'
    # The argv comes from the image's fixed CMD, with no user input involved.
    os.execvp(sys.argv[1], sys.argv[1:])  # noqa: S606


if __name__ == '__main__':
    main()
