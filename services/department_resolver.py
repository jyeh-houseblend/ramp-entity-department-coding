"""Resolves the NetSuite Department accounting field and maps an employee's
Ramp department to one of its options.

Resolution order for an employee's department:
1. DEPARTMENT_MAP_FILE (JSON) keyed by Ramp department id or name. The value
   is an option ramp_id, ERP id/code, value or display name. A value of null
   means "never auto-code this department".
2. Name match: Ramp department name == option value/display_name
   (case/punctuation-insensitive), then a unique match on the leaf of a
   NetSuite hierarchical name ("Parent : Child" -> "Child").
Anything ambiguous or unmatched returns None: we never guess.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from client.models import AccountingField, FieldOption, FieldSelection, Transaction
from client.ramp_api import RampClient

from .cache import TTLCache

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    pass


def normalize(text: str | None) -> str:
    # "&" == "and", so "IT & Integration" matches NetSuite's "IT and Integration".
    text = (text or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def leaf(text: str | None) -> str:
    return normalize((text or "").split(":")[-1])


class DepartmentResolver:
    def __init__(self, client: RampClient, *, field_id: str = "", field_name: str = "Department",
                 map_file: str = "", cache_ttl_seconds: float = 900):
        self._client = client
        self._field_id = field_id.strip()
        self._field_name = field_name
        self._cache: TTLCache = TTLCache(cache_ttl_seconds)
        self._overrides = self._load_map(map_file)

    # ------------------------------------------------------------ reference

    @staticmethod
    def _load_map(map_file: str) -> dict[str, str | None]:
        if not map_file:
            return {}
        path = Path(map_file)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read DEPARTMENT_MAP_FILE {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigurationError("DEPARTMENT_MAP_FILE must be a JSON object")
        return {str(k): (None if v is None else str(v)) for k, v in data.items()}

    def field(self) -> AccountingField:
        return self._cache.get_or_load("field", self._find_field)

    def _find_field(self) -> AccountingField:
        fields = self._client.list_accounting_fields()
        if self._field_id:
            for f in fields:
                if self._field_id in {f.ramp_uuid, f.id, f.external_id}:
                    return f
            raise ConfigurationError(
                f"No accounting field with id {self._field_id!r} "
                "(NETSUITE_DEPARTMENT_TRACKING_CATEGORY_ID). Run scripts/discover_fields.py.")
        wanted = normalize(self._field_name)
        hits = [f for f in fields if wanted in {normalize(f.name), normalize(f.display_name)}]
        if len(hits) != 1:
            raise ConfigurationError(
                f"Expected exactly one accounting field named {self._field_name!r}, found "
                f"{len(hits)}. Set NETSUITE_DEPARTMENT_TRACKING_CATEGORY_ID to its ramp_id "
                "(see scripts/discover_fields.py).")
        return hits[0]

    def options(self) -> list[FieldOption]:
        def load() -> list[FieldOption]:
            field_ramp_id = self.field().ramp_uuid
            return [o for o in self._client.list_field_options(field_ramp_id)
                    if o.is_active is not False]
        return self._cache.get_or_load("options", load)

    def ramp_departments(self) -> dict[str, str]:
        return self._cache.get_or_load(
            "departments", lambda: {d.id: d.name or "" for d in self._client.list_departments()})

    # -------------------------------------------------------------- lookups

    def _is_department_selection(self, sel: FieldSelection) -> bool:
        field = self.field()
        info = sel.category_info
        if not info:
            return False
        return bool((info.id and info.id == field.ramp_uuid)
                    or (info.external_id and info.external_id == field.erp_id))

    def current_selection(self, txn: Transaction) -> FieldSelection | None:
        """The department already coded on the transaction or any line item."""
        selections = list(txn.accounting_field_selections)
        for li in txn.line_items:
            selections.extend(li.accounting_field_selections)
        for sel in selections:
            if self._is_department_selection(sel) and (sel.id or sel.external_id):
                return sel
        return None

    def _find_option(self, target: str) -> FieldOption | None:
        options = self.options()
        for o in options:  # exact id match first
            if target in {o.ramp_uuid, o.ramp_id, o.id, o.code}:
                return o
        return self._match_by_name(target)

    def _match_by_name(self, name: str) -> FieldOption | None:
        options = self.options()
        norm = normalize(name)
        exact = [o for o in options if norm and norm in {normalize(o.value), normalize(o.display_name)}]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            logger.warning("Department %r matches %d options; add it to DEPARTMENT_MAP_FILE",
                           name, len(exact))
            return None
        by_leaf = [o for o in options if norm and norm in {leaf(o.value), leaf(o.display_name)}]
        if len(by_leaf) == 1:
            return by_leaf[0]
        if len(by_leaf) > 1:
            logger.warning("Department %r matches %d hierarchical options; add it to "
                           "DEPARTMENT_MAP_FILE", name, len(by_leaf))
        return None

    def resolve(self, ramp_department_id: str | None) -> FieldOption | None:
        if not ramp_department_id:
            return None
        dept_name = self.ramp_departments().get(ramp_department_id, "")
        for key in (ramp_department_id, dept_name):
            if key and key in self._overrides:
                target = self._overrides[key]
                if target is None:
                    logger.info("Department %r is excluded from auto-coding by map file",
                                dept_name or ramp_department_id)
                    return None
                option = self._find_option(target)
                if not option:
                    logger.error("Map file sends department %r to %r but no active option "
                                 "matches", dept_name or ramp_department_id, target)
                return option
        return self._match_by_name(dept_name) if dept_name else None

    def department_name(self, ramp_department_id: str | None) -> str:
        return self.ramp_departments().get(ramp_department_id or "", "") if ramp_department_id else ""
