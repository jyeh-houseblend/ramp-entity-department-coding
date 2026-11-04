"""Orchestrates ingestion -> entity evaluation -> department coding."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from client.models import Transaction, User
from client.ramp_api import RampAPIError, RampClient

from .cache import TTLCache
from .department_resolver import DepartmentResolver
from .entity_matcher import EntityRelation, compare_entities
from .state_store import StateStore

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    APPLIED = "applied"
    WOULD_APPLY = "would_apply"                      # dry run
    MISMATCH = "mismatch_left_blank"                 # exception: reviewer picks dept
    MISMATCH_PREFILLED = "mismatch_has_department"   # exception, but already coded: review it
    ALREADY_CODED = "already_coded"
    ENTITY_UNKNOWN = "entity_unknown"
    NO_USER_DEPARTMENT = "user_has_no_department"
    NO_MAPPING = "no_department_mapping"
    SPLIT = "split_transaction"
    ERROR = "error"
    PREVIOUSLY_PROCESSED = "previously_processed"


# Outcomes that are final: the state store skips these on later runs. The
# others (missing mapping, API errors, ...) are retried once fixed.
TERMINAL = {Outcome.APPLIED, Outcome.ALREADY_CODED, Outcome.MISMATCH, Outcome.MISMATCH_PREFILLED}


@dataclass
class Result:
    transaction_id: str
    outcome: Outcome
    cardholder: str = ""
    merchant: str = ""
    amount: float | None = None
    transaction_time: str = ""
    user_entity: str = ""
    card_entity: str = ""
    department: str = ""
    detail: str = ""


@dataclass
class RunSummary:
    results: list[Result] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.outcome.value] = out.get(r.outcome.value, 0) + 1
        return out


class TransactionProcessor:
    def __init__(self, client: RampClient, resolver: DepartmentResolver,
                 state: StateStore | None = None, *, dry_run: bool = False,
                 cache_ttl_seconds: float = 900):
        self._client = client
        self._resolver = resolver
        self._state = state
        self._dry_run = dry_run
        self._users: TTLCache[User] = TTLCache(cache_ttl_seconds)
        self._fund_entities: TTLCache[str | None] = TTLCache(cache_ttl_seconds)
        self._entity_names: dict[str, str] | None = None

    # -------------------------------------------------------------- helpers

    def _entity_label(self, entity_id: str | None) -> str:
        if not entity_id:
            return ""
        if self._entity_names is None:
            try:
                self._entity_names = {e.id: e.entity_name or "" for e in self._client.list_entities()}
            except RampAPIError as exc:  # e.g. entities:read not granted
                logger.debug("Entity names unavailable (%s); reporting ids", exc)
                self._entity_names = {}
        name = self._entity_names.get(entity_id)
        return f"{name} ({entity_id})" if name else entity_id

    def _user(self, user_id: str) -> User:
        return self._users.get_or_load(user_id, lambda: self._client.get_user(user_id))

    def _card_entity_id(self, txn: Transaction) -> str | None:
        if txn.entity_id:
            return txn.entity_id
        if txn.fund_id:
            return self._fund_entities.get_or_load(
                txn.fund_id, lambda: self._client.get_fund(txn.fund_id).entity_id)
        return None

    # ----------------------------------------------------------- evaluation

    def process_transaction(self, txn: Transaction, *, reprocess: bool = False) -> Result:
        result = Result(txn.id, Outcome.ERROR, cardholder=txn.cardholder_name,
                        merchant=txn.merchant_name or "", amount=txn.amount,
                        transaction_time=txn.user_transaction_time or "")
        if self._state and not reprocess:
            previous = self._state.get(txn.id)
            if previous:
                result.outcome, result.detail = Outcome.PREVIOUSLY_PROCESSED, previous
                return result
        try:
            self._evaluate(txn, result)
        except RampAPIError as exc:
            result.outcome, result.detail = Outcome.ERROR, str(exc)
            logger.error("txn %s: %s", txn.id, exc)

        log = logger.warning if result.outcome in {
            Outcome.MISMATCH, Outcome.MISMATCH_PREFILLED, Outcome.NO_MAPPING,
            Outcome.ENTITY_UNKNOWN} else logger.info
        log("txn %s [%s] user=%s card=%s dept=%s %s", txn.id, result.outcome.value,
            result.user_entity or "-", result.card_entity or "-", result.department or "-",
            result.detail)

        if self._state and not self._dry_run and result.outcome in TERMINAL:
            self._state.record(txn.id, result.outcome.value, result.department or None)
        return result

    def _evaluate(self, txn: Transaction, result: Result) -> None:
        user_id = txn.card_holder.user_id if txn.card_holder else None
        user = self._user(user_id) if user_id else None
        comparison = compare_entities(user.entity_id if user else None, self._card_entity_id(txn))
        result.user_entity = self._entity_label(comparison.user_entity_id)
        result.card_entity = self._entity_label(comparison.card_entity_id)
        current = self._resolver.current_selection(txn)

        if comparison.relation is EntityRelation.UNKNOWN:
            result.outcome = Outcome.ENTITY_UNKNOWN
            result.detail = "missing user or card entity; not auto-coded"
            return

        if comparison.relation is EntityRelation.MISMATCH:
            if current:
                # Something (Ramp defaults, a rule, a person) already set it.
                # Flag for review rather than silently trusting or clearing it.
                result.outcome = Outcome.MISMATCH_PREFILLED
                result.department = current.name or current.external_id or ""
                result.detail = "entity mismatch but department already set; review it"
            else:
                result.outcome = Outcome.MISMATCH
                result.detail = "entity mismatch; department left blank for manual selection"
            return

        # --- entities match ---
        if current:
            result.outcome = Outcome.ALREADY_CODED
            result.department = current.name or current.external_id or ""
            return
        if len(txn.line_items) > 1:
            result.outcome = Outcome.SPLIT
            result.detail = f"{len(txn.line_items)} split lines; code manually"
            return
        if not user or not user.department_id:
            result.outcome = Outcome.NO_USER_DEPARTMENT
            result.detail = "employee has no default department in Ramp"
            return

        option = self._resolver.resolve(user.department_id)
        dept_name = self._resolver.department_name(user.department_id)
        if not option:
            result.outcome = Outcome.NO_MAPPING
            result.detail = f"no NetSuite department option for Ramp department {dept_name!r}"
            return
        result.department = option.label

        if self._dry_run:
            result.outcome = Outcome.WOULD_APPLY
            result.detail = f"would code department from Ramp department {dept_name!r}"
            return

        field_ramp_id = self._resolver.field().ramp_uuid
        before = _coded_fields(txn)
        self._client.post_accounting_coding(txn.id, field_ramp_id, option.ramp_uuid)
        result.outcome = Outcome.APPLIED
        self._verify_write(txn.id, before, result)

    def _verify_write(self, txn_id: str, before: set[str], result: Result) -> None:
        """Re-read the transaction: the department must now be set and no
        other previously coded field may have been dropped."""
        after_txn = self._client.get_transaction(txn_id)
        if not self._resolver.current_selection(after_txn):
            result.outcome, result.detail = Outcome.ERROR, "write accepted but department not set"
            return
        dropped = before - _coded_fields(after_txn)
        if dropped:
            result.outcome = Outcome.ERROR
            result.detail = f"coding write dropped other fields: {sorted(dropped)}"
            logger.critical("txn %s: POST /accounting/codings removed fields %s. Stop and "
                            "investigate before running again.", txn_id, sorted(dropped))

    # ---------------------------------------------------------------- runs

    def run(self, transactions: Iterable[Transaction], *, reprocess: bool = False) -> RunSummary:
        summary = RunSummary()
        for txn in transactions:
            summary.results.append(self.process_transaction(txn, reprocess=reprocess))
            if summary.results[-1].detail.startswith("coding write dropped"):
                logger.critical("Aborting run after field-loss detection")
                break
        return summary


def _coded_fields(txn: Transaction) -> set[str]:
    out = set()
    for sel in txn.accounting_field_selections:
        info = sel.category_info
        if info and (info.id or info.external_id):
            out.add(info.id or info.external_id)
    return out
