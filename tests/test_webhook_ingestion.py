"""End-to-end: signed webhook -> background task -> real TransactionProcessor
against the fake Ramp API. TestClient runs background tasks before returning."""
import json

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.routes import WebhookService
from server.security import compute_signature
from services.department_resolver import DepartmentResolver
from services.state_store import StateStore
from services.transaction_processor import TransactionProcessor

from .conftest import DEPT_FIELD_RAMP_ID, GL_FIELD_RAMP_ID, PARENT, PORTCO_A, dept_selection, make_txn

SECRET = "topsecret"


def build(client, *, dry_run=False, state=None):
    resolver = DepartmentResolver(client, field_id=DEPT_FIELD_RAMP_ID)
    processor = TransactionProcessor(client, resolver, state, dry_run=dry_run)
    service = WebhookService(processor, client, client, SECRET)
    return service, TestClient(create_app(service))


def send(http, txn_id, *, event_id=None, event_type="transactions.cleared"):
    body = json.dumps({"id": event_id or f"evt-{txn_id}", "type": event_type,
                       "object": {"id": txn_id}}).encode()
    return http.post("/webhooks/ramp", content=body,
                     headers={"X-Ramp-Signature": compute_signature(body, SECRET)})


@pytest.fixture
def http(client):
    return build(client)[1]


def test_cleared_matching_entities_codes_department(fake, http):
    fake.transactions["t1"] = make_txn("t1", "u-parent", PARENT)
    assert send(http, "t1").status_code == 202
    assert fake.codings == [{
        "object_type": "TRANSACTION", "object_id": "t1",
        "accounting_coding_selections": [{"accounting_field_id": DEPT_FIELD_RAMP_ID,
                                          "accounting_field_option_id": "opt-sales-ramp"}]}]


def test_cleared_entity_mismatch_leaves_department_blank(fake, http):
    fake.transactions["t2"] = make_txn("t2", "u-parent", PORTCO_A)
    assert send(http, "t2").status_code == 202
    assert fake.codings == []


def test_existing_department_is_not_overwritten(fake, http):
    fake.transactions["t3"] = make_txn("t3", "u-parent", PARENT, selections=[dept_selection()])
    send(http, "t3")
    assert fake.codings == []


@pytest.mark.parametrize("event_type", ["transactions.authorized", "bills.created"])
def test_other_event_types_are_ignored(fake, http, event_type):
    fake.transactions["t4"] = make_txn("t4", "u-parent", PARENT)
    assert send(http, "t4", event_type=event_type).status_code == 200
    assert ("GET", "/developer/v1/transactions/t4") not in fake.calls
    assert fake.codings == []


def test_retried_event_id_is_processed_once(fake, http):
    fake.transactions["t5"] = make_txn("t5", "u-parent", PARENT)
    send(http, "t5", event_id="evt-same")
    send(http, "t5", event_id="evt-same")
    assert fake.calls.count(("GET", "/developer/v1/transactions/t5")) == 2  # fetch + verify read
    assert len(fake.codings) == 1


def test_state_store_blocks_second_write_for_new_event_id(fake, client, tmp_path):
    _, http = build(client, state=StateStore(str(tmp_path / "s.db")))
    fake.transactions["t6"] = make_txn("t6", "u-parent", PARENT)
    send(http, "t6", event_id="evt-a")
    send(http, "t6", event_id="evt-b")  # e.g. cleared again after a refund
    assert len(fake.codings) == 1


def test_dry_run_writes_nothing(fake, client):
    _, http = build(client, dry_run=True)
    fake.transactions["t7"] = make_txn("t7", "u-parent", PARENT)
    send(http, "t7")
    assert fake.codings == []


def test_field_loss_halts_further_writes(fake, client):
    service, http = build(client)
    fake.drop_other_fields_on_write = True
    gl = {"id": "gl-opt", "external_id": "6000", "name": "Meals",
          "category_info": {"id": GL_FIELD_RAMP_ID, "external_id": "ns-gl"}}
    fake.transactions["t8"] = make_txn("t8", "u-parent", PARENT, selections=[gl])
    fake.transactions["t9"] = make_txn("t9", "u-parent", PARENT)
    send(http, "t8")
    assert service.halted
    send(http, "t9")
    assert [c["object_id"] for c in fake.codings] == ["t8"]


def test_ramp_api_error_is_logged_not_raised(fake, http):
    # The transaction fetch 404s; the handler must log it, not raise, so one
    # bad event can't take down the worker.
    fake.fail_next = [404]
    assert send(http, "missing").status_code == 202
    assert fake.codings == []


def test_malformed_body_is_rejected(http):
    assert http.post("/webhooks/ramp", content=b"not json").status_code == 400
