import json

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.routes import WebhookService
from server.security import compute_signature, verify_signature
from services.department_resolver import DepartmentResolver
from services.transaction_processor import TransactionProcessor

from .conftest import DEPT_FIELD_RAMP_ID, PARENT, make_txn

SECRET = "topsecret"


def test_verify_signature_accepts_matching_hmac():
    body = b'{"id": "evt-1"}'
    assert verify_signature(body, compute_signature(body, SECRET), SECRET)


@pytest.mark.parametrize("signature", [None, "", "deadbeef"])
def test_verify_signature_rejects_missing_or_wrong(signature):
    assert not verify_signature(b'{"id": "evt-1"}', signature, SECRET)


def test_signature_is_over_raw_bytes_not_parsed_json():
    signed = b'{"a": 1, "b": 2}'
    reordered = b'{"b": 2, "a": 1}'
    assert not verify_signature(reordered, compute_signature(signed, SECRET), SECRET)


@pytest.fixture
def http(fake, client):
    resolver = DepartmentResolver(client, field_id=DEPT_FIELD_RAMP_ID)
    service = WebhookService(TransactionProcessor(client, resolver), client, client, SECRET)
    return TestClient(create_app(service))


def _event(txn_id="t1", event_id="evt-1"):
    return json.dumps({"id": event_id, "type": "transactions.cleared",
                       "object": {"id": txn_id}}).encode()


def test_unsigned_event_is_rejected_and_not_processed(fake, http):
    fake.transactions["t1"] = make_txn("t1", "u-parent", PARENT)
    response = http.post("/webhooks/ramp", content=_event())
    assert response.status_code == 401
    assert fake.codings == []


def test_badly_signed_event_is_rejected(fake, http):
    fake.transactions["t1"] = make_txn("t1", "u-parent", PARENT)
    response = http.post("/webhooks/ramp", content=_event(),
                         headers={"X-Ramp-Signature": compute_signature(b"other", SECRET)})
    assert response.status_code == 401
    assert fake.codings == []


def test_correctly_signed_event_is_accepted(fake, http):
    fake.transactions["t1"] = make_txn("t1", "u-parent", PARENT)
    body = _event()
    response = http.post("/webhooks/ramp", content=body,
                         headers={"X-Ramp-Signature": compute_signature(body, SECRET)})
    assert response.status_code == 202
    assert len(fake.codings) == 1


def test_verification_handshake_needs_no_signature(fake, http):
    fake.webhooks = [{"id": "wh-active", "status": "active"},
                     {"id": "wh-new", "status": "pending_verification"}]
    response = http.post("/webhooks/ramp", content=json.dumps(
        {"type": "webhooks.verification", "challenge": "abc123"}).encode())
    assert response.status_code == 200
    assert fake.verified == [("wh-new", "abc123")]


def test_health(http):
    assert http.get("/health").json() == {"status": "healthy"}
