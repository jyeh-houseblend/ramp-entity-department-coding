"""Core business rule: compare the cardholder's home entity with the card's
entity.

- Match    (user.entity_id == card entity): auto-code the NetSuite Department
            from the employee's default department.
- Mismatch (user.entity_id != card entity): do NOT auto-code; leave it blank
            for the employee / finance reviewer to pick a department valid for
            the card's entity.
- Unknown  (either side missing): treated as an exception, never auto-coded.

"Card entity" here is the entity the spend books to: `transaction.entity_id`,
which Ramp derives from the card's fund. Ramp's current card APIs no longer
expose entity_id on the card itself; it lives on the Fund
(GET /funds/{fund_id}), which is the fallback when the transaction lacks it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EntityRelation(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EntityComparison:
    relation: EntityRelation
    user_entity_id: str | None
    card_entity_id: str | None

    @property
    def should_autofill(self) -> bool:
        return self.relation is EntityRelation.MATCH


def compare_entities(user_entity_id: str | None, card_entity_id: str | None) -> EntityComparison:
    if not user_entity_id or not card_entity_id:
        relation = EntityRelation.UNKNOWN
    elif user_entity_id == card_entity_id:
        relation = EntityRelation.MATCH
    else:
        relation = EntityRelation.MISMATCH
    return EntityComparison(relation, user_entity_id, card_entity_id)
