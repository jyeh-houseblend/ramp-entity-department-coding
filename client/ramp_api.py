"""Typed HTTP client for the Ramp Developer API endpoints this tool uses.

Retries 429 / 5xx / network errors with exponential backoff (honouring
Retry-After), refreshes the token once on 401, and follows `page.next`
cursor pagination.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterator

import httpx

from .auth import RampAuth
from .models import (
    AccountingField,
    Department,
    Entity,
    FieldOption,
    Fund,
    Transaction,
    User,
)

logger = logging.getLogger(__name__)

API = "/developer/v1"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RampAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RampClient:
    def __init__(
        self,
        base_url: str,
        auth: RampAuth,
        http: httpx.Client | None = None,
        max_retries: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._http = http or httpx.Client(timeout=30)
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep

    # ------------------------------------------------------------------ core

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url  # page.next is already absolute
        return f"{self._base_url}{path_or_url}"

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), self._backoff_max)
                except ValueError:
                    pass
        delay = self._backoff_base * (2 ** attempt)
        return min(delay, self._backoff_max) * random.uniform(0.8, 1.2)

    def request(self, method: str, path: str, *, params: dict | None = None,
                json: Any = None) -> Any:
        url = self._url(path)
        reauthed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self._auth.get_token()}",
                       "Accept": "application/json"}
            response: httpx.Response | None = None
            try:
                response = self._http.request(method, url, params=params, json=json,
                                              headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise RampAPIError(f"{method} {path} failed: {exc}") from exc
                delay = self._backoff(attempt, None)
                logger.warning("%s %s network error (%s); retry %d in %.1fs",
                               method, path, exc, attempt + 1, delay)
                self._sleep(delay)
                attempt += 1
                continue

            if response.status_code == 401 and not reauthed:
                logger.info("401 from Ramp; refreshing token and retrying once")
                self._auth.invalidate()
                reauthed = True
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                delay = self._backoff(attempt, response)
                logger.warning("%s %s -> %d; retry %d/%d in %.1fs", method, path,
                               response.status_code, attempt + 1, self._max_retries, delay)
                self._sleep(delay)
                attempt += 1
                continue

            if response.is_error:
                raise RampAPIError(
                    f"{method} {path} -> {response.status_code}: {response.text[:500]}",
                    status_code=response.status_code, body=response.text,
                )
            if not response.content:
                return None
            return response.json()

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        url: str | None = path
        while url:
            payload = self.request("GET", url, params=params) or {}
            yield from payload.get("data", [])
            url = (payload.get("page") or {}).get("next")
            params = None  # `next` already carries every query parameter

    # ------------------------------------------------------------- endpoints

    def list_transactions(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        state: str | None = "CLEARED",
        has_no_sync_commits: bool | None = True,
        page_size: int = 100,
    ) -> Iterator[Transaction]:
        params: dict[str, Any] = {"page_size": page_size}
        if state:
            params["state"] = state
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        if has_no_sync_commits is not None:
            params["has_no_sync_commits"] = str(has_no_sync_commits).lower()
        for item in self.paginate(f"{API}/transactions", params):
            yield Transaction.model_validate(item)

    def get_transaction(self, transaction_id: str) -> Transaction:
        return Transaction.model_validate(self.request("GET", f"{API}/transactions/{transaction_id}"))

    def get_user(self, user_id: str) -> User:
        return User.model_validate(self.request("GET", f"{API}/users/{user_id}"))

    def get_fund(self, fund_id: str) -> Fund:
        return Fund.model_validate(self.request("GET", f"{API}/funds/{fund_id}"))

    def list_departments(self) -> list[Department]:
        return [Department.model_validate(d)
                for d in self.paginate(f"{API}/departments", {"page_size": 100})]

    def list_entities(self) -> list[Entity]:
        return [Entity.model_validate(e)
                for e in self.paginate(f"{API}/entities", {"page_size": 100})]

    def list_accounting_fields(self) -> list[AccountingField]:
        return [AccountingField.model_validate(f)
                for f in self.paginate(f"{API}/accounting/fields", {"page_size": 100})]

    def list_field_options(self, field_ramp_id: str) -> list[FieldOption]:
        return [FieldOption.model_validate(o) for o in self.paginate(
            f"{API}/accounting/field-options", {"field_id": field_ramp_id, "page_size": 100})]

    def post_accounting_coding(self, transaction_id: str, field_ramp_id: str,
                               option_ramp_id: str) -> Any:
        """Transaction-level coding. Takes Ramp ids (ramp_id), not ERP ids."""
        return self.request("POST", f"{API}/accounting/codings", json={
            "object_type": "TRANSACTION",
            "object_id": transaction_id,
            "accounting_coding_selections": [{
                "accounting_field_id": field_ramp_id,
                "accounting_field_option_id": option_ramp_id,
            }],
        })

    # ------------------------------------------------- webhook subscriptions
    # Used only by the webhook server's verification handshake and
    # scripts/register_webhook.py; needs webhooks:read / webhooks:write.

    def list_webhooks(self) -> list[dict]:
        payload = self.request("GET", f"{API}/webhooks") or []
        return payload if isinstance(payload, list) else payload.get("data", [])

    def verify_webhook(self, webhook_id: str, challenge: str) -> Any:
        return self.request("POST", f"{API}/webhooks/{webhook_id}/verify",
                            json={"challenge": challenge})

    def create_webhook(self, endpoint_url: str, event_types: list[str]) -> dict:
        return self.request("POST", f"{API}/webhooks",
                            json={"endpoint_url": endpoint_url, "event_types": event_types})
