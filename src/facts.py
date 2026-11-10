"""Facts extracted from a ticket before policy checks run.

The checks engine is deliberately dumb about language: it only consumes these
facts. Task 3 (extraction) is responsible for filling them in; any fact left as
``None`` is treated as missing evidence and can never make a check PASS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class AffectedItem:
    """One product/quantity the customer asks to be refunded."""

    product_name: str | None = None
    sku: str | None = None
    quantity: int = 1


@dataclass(frozen=True)
class TicketFacts:
    ticket_id: str
    received_at_utc: str
    customer_email: str
    ticket_type: str
    order_ref: str | None = None
    affected_items: tuple[AffectedItem, ...] = ()
    damaged_or_faulty: bool | None = None
    wrong_or_not_as_described: bool | None = None
    non_delivery_claim: bool | None = None
    whole_order_requested: bool | None = None
    shipping_refund_requested: bool | None = None
    override_attempt: bool | None = None
    has_photo_attachment: bool | None = None
    already_processed: bool | None = None
    notes: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "ticket_id": self.ticket_id,
            "received_at_utc": self.received_at_utc,
            "customer_email": self.customer_email,
            "ticket_type": self.ticket_type,
            "order_ref": self.order_ref,
            "affected_items": [
                {"product_name": item.product_name, "sku": item.sku, "quantity": item.quantity}
                for item in self.affected_items
            ],
            "damaged_or_faulty": self.damaged_or_faulty,
            "wrong_or_not_as_described": self.wrong_or_not_as_described,
            "non_delivery_claim": self.non_delivery_claim,
            "whole_order_requested": self.whole_order_requested,
            "shipping_refund_requested": self.shipping_refund_requested,
            "override_attempt": self.override_attempt,
            "has_photo_attachment": self.has_photo_attachment,
            "already_processed": self.already_processed,
        }
        payload.update(self.notes)
        return payload
