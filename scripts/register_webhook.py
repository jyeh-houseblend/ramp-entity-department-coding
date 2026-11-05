"""One-time setup: register the Ramp webhook subscription.

Run AFTER the service is deployed and reachable - Ramp immediately POSTs a
verification challenge to the endpoint, which the server answers itself:

    python -m scripts.register_webhook https://<your-app>.onrender.com/webhooks/ramp

Prints the subscription id and secret. Put the secret in RAMP_WEBHOOK_SECRET
(Render env and local .env), then redeploy so signature verification turns on.

Ramp can't add event types to an existing subscription and rejects a second
subscription for an endpoint_url already in use. To change event types,
delete the old subscription and register again.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.auth import RampAuth  # noqa: E402
from client.ramp_api import RampClient  # noqa: E402
from config.settings import load_settings  # noqa: E402
from server.routes import TRANSACTION_EVENTS  # noqa: E402

# Creating a subscription needs webhooks:write plus the read scope of each
# event it covers.
REGISTER_SCOPES = "webhooks:read webhooks:write transactions:read"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("endpoint_url", help="Public URL ending in /webhooks/ramp")
    p.add_argument("--env-file", default=".env")
    args = p.parse_args(argv)

    settings = load_settings(args.env_file)
    client = RampClient(settings.base_url, RampAuth(
        settings.client_id, settings.client_secret, settings.base_url, REGISTER_SCOPES))
    event_types = sorted(TRANSACTION_EVENTS)
    subscription = client.create_webhook(args.endpoint_url, event_types)

    print(f"Environment:     {settings.environment}")
    print(f"Event types:     {event_types}")
    print(f"Subscription id: {subscription['id']}")
    print(f"Status:          {subscription.get('status')}")
    print(f"Secret:          {subscription.get('secret')}")
    print()
    print("Set RAMP_WEBHOOK_SECRET to the secret above, then redeploy.")
    print("The server answers Ramp's verification challenge itself; check its logs for")
    print("'Webhook subscription ... verified' (status should then be 'active').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
