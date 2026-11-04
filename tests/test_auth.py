import base64

import httpx
import pytest

from client.auth import RampAuth, RampAuthError
from client.ramp_api import RampAPIError, RampClient

BASE = "https://demo-api.ramp.com"


def token_transport(record: list, status: int = 200, expires_in: int = 3600):
    def handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "invalid_scope"})
        return httpx.Response(200, json={"access_token": f"tok{len(record)}",
                                         "expires_in": expires_in})
    return httpx.MockTransport(handler)


def test_token_request_uses_basic_auth_and_scopes():
    seen: list = []
    auth = RampAuth("my-id", "my-secret", BASE, "transactions:read users:read",
                    http=httpx.Client(transport=token_transport(seen)))
    assert auth.get_token() == "tok1"

    req = seen[0]
    assert str(req.url) == f"{BASE}/developer/v1/token"
    expected = base64.b64encode(b"my-id:my-secret").decode()
    assert req.headers["Authorization"] == f"Basic {expected}"
    body = req.content.decode()
    assert "grant_type=client_credentials" in body
    assert "scope=transactions%3Aread+users%3Aread" in body


def test_token_is_cached_until_near_expiry():
    seen: list = []
    now = [1000.0]
    auth = RampAuth("id", "s", BASE, "x", http=httpx.Client(transport=token_transport(seen)),
                    refresh_margin_seconds=60, clock=lambda: now[0])
    assert auth.get_token() == "tok1"
    now[0] += 3000  # still > 60s before expiry
    assert auth.get_token() == "tok1"
    now[0] += 560   # inside the refresh margin
    assert auth.get_token() == "tok2"
    assert len(seen) == 2


def test_token_error_raises():
    auth = RampAuth("id", "s", BASE, "x", http=httpx.Client(transport=token_transport([], 400)))
    with pytest.raises(RampAuthError, match="400"):
        auth.get_token()


def test_401_refreshes_token_once(fake, client):
    fake.fail_next = [401]
    client.get_user("u-parent")
    assert fake.token_requests == 2


def test_retries_429_and_5xx_then_succeeds(fake, client):
    fake.fail_next = [429, 503, 500]
    user = client.get_user("u-parent")
    assert user.entity_id == "ent-parent"
    assert fake.calls.count(("GET", "/developer/v1/users/u-parent")) == 4


def test_gives_up_after_max_retries(fake, client):
    fake.fail_next = [503] * 10
    with pytest.raises(RampAPIError) as exc:
        client.get_user("u-parent")
    assert exc.value.status_code == 503


def test_backoff_honours_retry_after_and_grows():
    auth = RampAuth("id", "s", BASE, "x")
    c = RampClient(BASE, auth, backoff_base_seconds=1, backoff_max_seconds=30)
    assert c._backoff(0, httpx.Response(429, headers={"Retry-After": "7"})) == 7
    assert 0.8 <= c._backoff(0, None) <= 1.2
    assert 6.4 <= c._backoff(3, None) <= 9.6
    assert c._backoff(10, None) <= 36
