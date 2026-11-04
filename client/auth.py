"""OAuth 2.0 Client Credentials token management for the Ramp Developer API."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

TOKEN_PATH = "/developer/v1/token"


class RampAuthError(RuntimeError):
    pass


class RampAuth:
    """Fetches and caches a bearer token, refreshing it shortly before expiry.

    Ramp authenticates the token request with HTTP Basic auth (client id /
    secret). Client Credentials grants return no refresh token, so "refresh"
    means requesting a new token.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        scopes: str,
        http: httpx.Client | None = None,
        refresh_margin_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = base_url.rstrip("/") + TOKEN_PATH
        self._scopes = scopes
        self._http = http or httpx.Client(timeout=15)
        self._margin = refresh_margin_seconds
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - self._margin:
                return self._token
            return self._fetch()

    def invalidate(self) -> None:
        """Force the next get_token() to fetch a new token (e.g. after a 401)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> str:
        try:
            response = self._http.post(
                self._token_url,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials", "scope": self._scopes},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise RampAuthError(f"Token request failed: {exc}") from exc

        if response.status_code != 200:
            # Body is an OAuth error ({"error": "invalid_scope", ...}); safe to log.
            raise RampAuthError(
                f"Token request returned {response.status_code}: {response.text[:500]}"
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = self._clock() + int(payload.get("expires_in", 3600))
        logger.debug("Obtained Ramp access token (expires in %ss)", payload.get("expires_in"))
        return self._token
