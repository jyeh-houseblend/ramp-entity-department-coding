"""Typed views of the Ramp API objects this tool reads. Unknown fields are
ignored so additive API changes don't break parsing."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CategoryInfo(_Model):
    id: str | None = None           # Ramp id of the accounting field
    external_id: str | None = None  # ERP id of the accounting field
    name: str | None = None
    type: str | None = None


class FieldSelection(_Model):
    id: str | None = None           # Ramp id of the selected option
    external_id: str | None = None  # ERP id of the selected option
    name: str | None = None
    category_info: CategoryInfo | None = None


class LineItem(_Model):
    id: str | None = None
    accounting_field_selections: list[FieldSelection] = []


class CardHolder(_Model):
    user_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    department_id: str | None = None
    department_name: str | None = None


class Transaction(_Model):
    id: str
    state: str | None = None
    amount: float | None = None
    merchant_name: str | None = None
    user_transaction_time: str | None = None
    card_id: str | None = None
    fund_id: str | None = None
    entity_id: str | None = None
    card_holder: CardHolder | None = None
    accounting_field_selections: list[FieldSelection] = []
    line_items: list[LineItem] = []

    @property
    def cardholder_name(self) -> str:
        ch = self.card_holder
        return " ".join(p for p in (ch.first_name, ch.last_name) if p) if ch else ""


class User(_Model):
    id: str
    entity_id: str | None = None
    department_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


class Fund(_Model):
    id: str
    entity_id: str | None = None


class Department(_Model):
    id: str
    name: str | None = None


class Entity(_Model):
    id: str
    entity_name: str | None = None


class AccountingField(_Model):
    # On NetSuite-synced accounts `id` holds the ERP id and `ramp_id` the Ramp id.
    id: str | None = None
    ramp_id: str | None = None
    external_id: str | None = None
    name: str | None = None
    display_name: str | None = None

    @property
    def ramp_uuid(self) -> str | None:
        return self.ramp_id or self.id

    @property
    def erp_id(self) -> str | None:
        return self.external_id or self.id


class FieldOption(_Model):
    id: str | None = None
    ramp_id: str | None = None
    code: str | None = None
    value: str | None = None
    display_name: str | None = None
    is_active: bool | None = True

    @property
    def ramp_uuid(self) -> str | None:
        return self.ramp_id or self.id

    @property
    def label(self) -> str:
        return self.display_name or self.value or self.code or self.ramp_uuid or "?"
