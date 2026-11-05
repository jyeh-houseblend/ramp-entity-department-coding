"""A small in-memory fake of the Ramp API, served through httpx.MockTransport
so tests exercise the real RampClient (auth, pagination, retries)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from client.auth import RampAuth
from client.ramp_api import RampClient

BASE = "https://demo-api.ramp.com"
PARENT, PORTCO_A = "ent-parent", "ent-portco-a"
DEPT_FIELD_RAMP_ID = "field-dept-ramp"
DEPT_FIELD_ERP_ID = "ns-dept"
GL_FIELD_RAMP_ID = "field-gl-ramp"


@dataclass
class FakeRamp:
    transactions: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)
    funds: dict = field(default_factory=dict)
    departments: list = field(default_factory=list)
    options: list = field(default_factory=list)
    codings: list = field(default_factory=list)       # POST /accounting/codings bodies
    calls: list = field(default_factory=list)          # (method, path)
    fail_next: list = field(default_factory=list)      # status codes to return first
    token_requests: int = 0
    drop_other_fields_on_write: bool = False
    webhooks: list = field(default_factory=list)       # GET /webhooks
    verified: list = field(default_factory=list)       # (webhook_id, challenge)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/developer/v1/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}",
                                             "expires_in": 3600})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"})

        p = path.removeprefix("/developer/v1")
        if p == "/transactions":
            return httpx.Response(200, json={"data": list(self.transactions.values()),
                                             "page": {"next": None}})
        if p.startswith("/transactions/"):
            return httpx.Response(200, json=self.transactions[p.split("/")[-1]])
        if p.startswith("/users/"):
            return httpx.Response(200, json=self.users[p.split("/")[-1]])
        if p.startswith("/funds/"):
            return httpx.Response(200, json=self.funds[p.split("/")[-1]])
        if p == "/departments":
            return httpx.Response(200, json={"data": self.departments, "page": {"next": None}})
        if p == "/entities":
            return httpx.Response(200, json={"data": [
                {"id": PARENT, "entity_name": "Parent Co"},
                {"id": PORTCO_A, "entity_name": "PortCo A"}], "page": {"next": None}})
        if p == "/accounting/fields":
            return httpx.Response(200, json={"data": [
                {"id": DEPT_FIELD_ERP_ID, "ramp_id": DEPT_FIELD_RAMP_ID, "name": "Department"},
                {"id": "ns-gl", "ramp_id": GL_FIELD_RAMP_ID, "name": "GL Account"},
            ], "page": {"next": None}})
        if p == "/accounting/field-options":
            assert request.url.params["field_id"] == DEPT_FIELD_RAMP_ID
            return httpx.Response(200, json={"data": self.options, "page": {"next": None}})
        if p == "/webhooks" and request.method == "GET":
            return httpx.Response(200, json=self.webhooks)
        if p.startswith("/webhooks/") and p.endswith("/verify"):
            self.verified.append((p.split("/")[2], json.loads(request.content)["challenge"]))
            return httpx.Response(200, json={"success": True})
        if p == "/accounting/codings" and request.method == "POST":
            body = json.loads(request.content)
            self.codings.append(body)
            self._apply_coding(body)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})

    def _apply_coding(self, body: dict) -> None:
        txn = self.transactions[body["object_id"]]
        sel = body["accounting_coding_selections"][0]
        option = next(o for o in self.options if o["ramp_id"] == sel["accounting_field_option_id"])
        new = {"id": option["ramp_id"], "external_id": option["id"], "name": option["value"],
               "category_info": {"id": DEPT_FIELD_RAMP_ID, "external_id": DEPT_FIELD_ERP_ID}}
        kept = [] if self.drop_other_fields_on_write else txn["accounting_field_selections"]
        txn["accounting_field_selections"] = kept + [new]


def make_txn(txn_id: str, user_id: str, entity_id: str | None, *, selections=None,
             line_items=None, fund_id: str | None = "fund-1") -> dict:
    return {"id": txn_id, "state": "CLEARED", "amount": 42.0, "merchant_name": "Acme",
            "entity_id": entity_id, "fund_id": fund_id, "card_id": "card-1",
            "card_holder": {"user_id": user_id, "first_name": "Pat", "last_name": "Lee"},
            "accounting_field_selections": selections or [],
            "line_items": line_items or []}


def dept_selection(option_ramp_id="opt-sales-ramp"):
    return {"id": option_ramp_id, "external_id": "10", "name": "Sales",
            "category_info": {"id": DEPT_FIELD_RAMP_ID, "external_id": DEPT_FIELD_ERP_ID}}


@pytest.fixture
def fake() -> FakeRamp:
    f = FakeRamp()
    f.users = {
        "u-parent": {"id": "u-parent", "entity_id": PARENT, "department_id": "d-sales"},
        "u-nodept": {"id": "u-nodept", "entity_id": PARENT, "department_id": None},
        "u-ops": {"id": "u-ops", "entity_id": PARENT, "department_id": "d-ops"},
    }
    f.funds = {"fund-portco": {"id": "fund-portco", "entity_id": PORTCO_A},
               "fund-parent": {"id": "fund-parent", "entity_id": PARENT}}
    f.departments = [{"id": "d-sales", "name": "Sales"}, {"id": "d-ops", "name": "Operations"}]
    f.options = [
        {"id": "10", "ramp_id": "opt-sales-ramp", "value": "Sales", "is_active": True},
        {"id": "20", "ramp_id": "opt-ops-ramp", "value": "G&A : Operations", "is_active": True},
    ]
    return f


@pytest.fixture
def client(fake) -> RampClient:
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    auth = RampAuth("cid", "secret", BASE, "transactions:read", http=http)
    return RampClient(BASE, auth, http=http, sleep=lambda s: None)
