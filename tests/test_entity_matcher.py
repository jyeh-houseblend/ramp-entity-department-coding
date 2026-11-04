import json

import pytest

from services.department_resolver import DepartmentResolver
from services.entity_matcher import EntityRelation, compare_entities
from services.state_store import StateStore
from services.transaction_processor import Outcome, TransactionProcessor
from client.models import Transaction

from .conftest import DEPT_FIELD_RAMP_ID, GL_FIELD_RAMP_ID, PARENT, PORTCO_A, dept_selection, make_txn


# ---------------------------------------------------------------- pure rule

@pytest.mark.parametrize("user, card, relation", [
    (PARENT, PARENT, EntityRelation.MATCH),
    (PARENT, PORTCO_A, EntityRelation.MISMATCH),
    (None, PARENT, EntityRelation.UNKNOWN),
    (PARENT, None, EntityRelation.UNKNOWN),
    ("", "", EntityRelation.UNKNOWN),
])
def test_compare_entities(user, card, relation):
    result = compare_entities(user, card)
    assert result.relation is relation
    assert result.should_autofill is (relation is EntityRelation.MATCH)


# ------------------------------------------------------- processor end-to-end

def processor(client, *, dry_run=False, state=None, map_file=""):
    resolver = DepartmentResolver(client, field_id=DEPT_FIELD_RAMP_ID, map_file=map_file)
    return TransactionProcessor(client, resolver, state, dry_run=dry_run)


def run_one(fake, client, txn_dict, **kw):
    fake.transactions[txn_dict["id"]] = txn_dict
    return processor(client, **kw).process_transaction(Transaction.model_validate(txn_dict))


def test_matching_entities_apply_department(fake, client):
    result = run_one(fake, client, make_txn("t1", "u-parent", PARENT))
    assert result.outcome is Outcome.APPLIED
    assert result.department == "Sales"
    assert fake.codings == [{
        "object_type": "TRANSACTION", "object_id": "t1",
        "accounting_coding_selections": [{"accounting_field_id": DEPT_FIELD_RAMP_ID,
                                          "accounting_field_option_id": "opt-sales-ramp"}]}]


def test_mismatched_entities_skip_department(fake, client):
    result = run_one(fake, client, make_txn("t2", "u-parent", PORTCO_A))
    assert result.outcome is Outcome.MISMATCH
    assert fake.codings == []
    assert "PortCo A" in result.card_entity and "Parent Co" in result.user_entity


def test_mismatch_with_prefilled_department_is_flagged_not_changed(fake, client):
    result = run_one(fake, client, make_txn("t3", "u-parent", PORTCO_A,
                                            selections=[dept_selection()]))
    assert result.outcome is Outcome.MISMATCH_PREFILLED
    assert fake.codings == []


def test_card_entity_falls_back_to_fund(fake, client):
    mismatch = run_one(fake, client, make_txn("t4", "u-parent", None, fund_id="fund-portco"))
    assert mismatch.outcome is Outcome.MISMATCH
    match = run_one(fake, client, make_txn("t5", "u-parent", None, fund_id="fund-parent"))
    assert match.outcome is Outcome.APPLIED


def test_unknown_entity_is_never_coded(fake, client):
    result = run_one(fake, client, make_txn("t6", "u-parent", None, fund_id=None))
    assert result.outcome is Outcome.ENTITY_UNKNOWN
    assert fake.codings == []


def test_existing_department_is_not_overwritten(fake, client):
    result = run_one(fake, client, make_txn("t7", "u-parent", PARENT,
                                            selections=[dept_selection("opt-ops-ramp")]))
    assert result.outcome is Outcome.ALREADY_CODED
    assert fake.codings == []


def test_split_transactions_are_left_for_manual_coding(fake, client):
    lines = [{"id": "l1", "accounting_field_selections": []},
             {"id": "l2", "accounting_field_selections": []}]
    result = run_one(fake, client, make_txn("t8", "u-parent", PARENT, line_items=lines))
    assert result.outcome is Outcome.SPLIT
    assert fake.codings == []


def test_user_without_department(fake, client):
    result = run_one(fake, client, make_txn("t9", "u-nodept", PARENT))
    assert result.outcome is Outcome.NO_USER_DEPARTMENT


def test_hierarchical_netsuite_name_matches_leaf(fake, client):
    result = run_one(fake, client, make_txn("t10", "u-ops", PARENT))
    assert result.outcome is Outcome.APPLIED
    assert result.department == "G&A : Operations"


def test_no_matching_option(fake, client):
    fake.options = [o for o in fake.options if o["value"] != "Sales"]
    result = run_one(fake, client, make_txn("t11", "u-parent", PARENT))
    assert result.outcome is Outcome.NO_MAPPING
    assert fake.codings == []


def test_map_file_overrides_and_excludes(fake, client, tmp_path):
    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps({"Sales": "20", "d-ops": None}))
    sales = run_one(fake, client, make_txn("t12", "u-parent", PARENT), map_file=str(map_file))
    assert sales.outcome is Outcome.APPLIED and sales.department == "G&A : Operations"
    ops = run_one(fake, client, make_txn("t13", "u-ops", PARENT), map_file=str(map_file))
    assert ops.outcome is Outcome.NO_MAPPING


def test_dry_run_writes_nothing(fake, client, tmp_path):
    state = StateStore(str(tmp_path / "s.db"))
    result = run_one(fake, client, make_txn("t14", "u-parent", PARENT), dry_run=True, state=state)
    assert result.outcome is Outcome.WOULD_APPLY
    assert fake.codings == []
    assert state.get("t14") is None
    assert not [c for c in fake.calls if c[0] != "GET" and c[1] != "/developer/v1/token"]


def test_state_store_skips_processed_transactions(fake, client, tmp_path):
    state = StateStore(str(tmp_path / "s.db"))
    txn = make_txn("t15", "u-parent", PARENT)
    assert run_one(fake, client, txn, state=state).outcome is Outcome.APPLIED
    again = run_one(fake, client, txn, state=state)
    assert again.outcome is Outcome.PREVIOUSLY_PROCESSED
    assert len(fake.codings) == 1


def test_field_loss_after_write_is_detected(fake, client):
    fake.drop_other_fields_on_write = True
    gl = {"id": "gl-opt", "external_id": "6000", "name": "Meals",
          "category_info": {"id": GL_FIELD_RAMP_ID, "external_id": "ns-gl"}}
    result = run_one(fake, client, make_txn("t16", "u-parent", PARENT, selections=[gl]))
    assert result.outcome is Outcome.ERROR
    assert "dropped" in result.detail


def test_user_lookups_are_cached(fake, client):
    proc = processor(client)
    for i in range(3):
        t = make_txn(f"c{i}", "u-parent", PORTCO_A)
        fake.transactions[t["id"]] = t
        proc.process_transaction(Transaction.model_validate(t))
    assert fake.calls.count(("GET", "/developer/v1/users/u-parent")) == 1
