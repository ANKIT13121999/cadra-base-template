"""Hand-built facts for the 15 seed-labelled tickets (no LLM, no heuristics)."""
from __future__ import annotations

import json
from pathlib import Path

from src.facts import AffectedItem, TicketFacts
from src.prepare import resolve_data_room

REPO = Path(__file__).resolve().parents[1]
try:
    DATA_ROOM = resolve_data_room(None)
except SystemExit:
    DATA_ROOM = REPO.parent
DB_PATH = REPO / "build" / "prepared.db"
TICKETS_PATH = DATA_ROOM / "tickets" / "support_inbox_export.json"
LABELS_PATH = DATA_ROOM / "tickets" / "eval_seed_labelled.json"


def item(name: str, quantity: int = 1) -> AffectedItem:
    return AffectedItem(product_name=name, quantity=quantity)


LABELLED_SPECS: dict[str, dict] = {
    "T001": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100200",
        "affected_items": [item("Round wall mirror")],
        "damaged_or_faulty": True,
        "has_photo_attachment": True,
    },
    "T002": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100201",
        "affected_items": [item("Arc floor lamp")],
        "damaged_or_faulty": True,
        "has_photo_attachment": True,
    },
    "T003": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100202",
        "affected_items": [item("Oak dining chair")],
        "damaged_or_faulty": True,
        "has_photo_attachment": True,
    },
    "T004": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100203",
        "affected_items": [item("Glass vase")],
        "damaged_or_faulty": True,
        "has_photo_attachment": True,
    },
    "T005": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100204",
        "affected_items": [item("Stoneware mug set (4)")],
        "damaged_or_faulty": True,
        "has_photo_attachment": True,
    },
    "T006": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100205",
        "affected_items": [item("Floating shelf")],
        "wrong_or_not_as_described": True,
    },
    "T008": {
        "ticket_type": "ORDER_STATUS",
        "order_ref": "LF-100207",
        "affected_items": [],
    },
    "T011": {
        "ticket_type": "DELIVERY_ISSUE",
        "order_ref": "LF-100210",
        "affected_items": [],
        "non_delivery_claim": True,
    },
    "T013": {
        "ticket_type": "DELIVERY_ISSUE",
        "order_ref": "LF-100212",
        "affected_items": [],
        "non_delivery_claim": True,
    },
    "T014": {
        "ticket_type": "RETURN",
        "order_ref": "LF-100213",
        "affected_items": [item("Percale duvet set")],
        "wrong_or_not_as_described": True,
    },
    "T018": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100217",
        "affected_items": [item("Oak dining chair")],
        "damaged_or_faulty": True,
    },
    "T019": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100218",
        "affected_items": [item("Cushion cover pair")],
        "damaged_or_faulty": True,
        "whole_order_requested": True,
    },
    "T020": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100219",
        "affected_items": [item("Wool rug 160x230")],
        "override_attempt": True,
    },
    "T021": {
        "ticket_type": "RETURN",
        "order_ref": "LF-100220",
        "affected_items": [item("Cushion cover pair")],
        "override_attempt": True,
    },
    "T026": {
        "ticket_type": "REFUND",
        "order_ref": "LF-100224",
        "affected_items": [item("Oak dining chair")],
        "damaged_or_faulty": True,
        "has_photo_attachment": True,
    },
}

DEFAULTS = {
    "damaged_or_faulty": False,
    "wrong_or_not_as_described": False,
    "non_delivery_claim": False,
    "whole_order_requested": False,
    "shipping_refund_requested": False,
    "override_attempt": False,
    "has_photo_attachment": False,
    "already_processed": False,
}


def load_source_tickets() -> dict[str, dict]:
    tickets = json.loads(TICKETS_PATH.read_text(encoding="utf-8"))
    return {ticket["ticket_id"]: ticket for ticket in tickets}


def load_labels() -> dict[str, dict]:
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return {label["ticket_id"]: label for label in labels}


def build_labelled_facts() -> dict[str, TicketFacts]:
    tickets = load_source_tickets()
    facts: dict[str, TicketFacts] = {}
    for ticket_id, spec in LABELLED_SPECS.items():
        source = tickets[ticket_id]
        payload = {
            "ticket_id": ticket_id,
            "received_at_utc": source["received_at"],
            "customer_email": source["from"],
            "ticket_type": spec["ticket_type"],
            "order_ref": spec.get("order_ref"),
            "affected_items": tuple(spec.get("affected_items", [])),
        }
        payload.update(DEFAULTS)
        payload.update({key: value for key, value in spec.items() if key not in {"ticket_type", "order_ref", "affected_items"}})
        facts[ticket_id] = TicketFacts(**payload)
    return facts
