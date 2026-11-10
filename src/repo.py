"""Read-only access to the prepared SQLite database.

No network, no LLM: the decision engine only reads these indexed structures.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


class PreparedData:
    """In-memory indexes over ``build/prepared.db``."""

    def __init__(
        self,
        customers: Iterable[dict[str, Any]],
        orders: Iterable[dict[str, Any]],
        order_items: Iterable[dict[str, Any]],
        shipments: Iterable[dict[str, Any]],
        refunds: Iterable[dict[str, Any]],
    ) -> None:
        self.customers: dict[str, dict[str, Any]] = {}
        self.customers_by_email: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.lines_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.shipment_by_order: dict[str, dict[str, Any]] = {}
        self.refunds_by_line: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        self.refunds_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for customer in customers:
            row = dict(customer)
            self.customers[row["customer_id"]] = row
            self.customers_by_email[row["email_norm"]] = row
        for order in orders:
            row = dict(order)
            if row.get("ambiguous_fields"):
                row["ambiguous_fields"] = json.loads(row["ambiguous_fields"])
            self.orders[row["order_id"]] = row
        for item in order_items:
            self.lines_by_order[item["order_id"]].append(dict(item))
        for lines in self.lines_by_order.values():
            lines.sort(key=lambda line: line["line_no"])
        for shipment in shipments:
            row = dict(shipment)
            row["flag_codes"] = json.loads(row.get("flag_codes") or "[]")
            if row.get("flag_detail"):
                row["flag_detail"] = json.loads(row["flag_detail"])
            self.shipment_by_order[row["order_id"]] = row
        for refund in refunds:
            record = dict(refund)
            self.refunds_by_line[(record["order_id"], int(refund["line_no"]))].append(record)
            self.refunds_by_order[record["order_id"]].append(record)

    @classmethod
    def from_sqlite(cls, path: str | Path) -> "PreparedData":
        db_path = Path(path)
        if not db_path.is_file():
            raise FileNotFoundError(f"prepared database not found: {db_path}; run `python -m src.prepare` first")
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            customers = [dict(row) for row in conn.execute("SELECT * FROM customers")]
            orders = [dict(row) for row in conn.execute("SELECT * FROM orders")]
            order_items = [dict(row) for row in conn.execute("SELECT * FROM order_items")]
            shipments = [dict(row) for row in conn.execute("SELECT * FROM shipments")]
            refunds = [dict(row) for row in conn.execute("SELECT * FROM refunds")]
        finally:
            conn.close()
        return cls(customers, orders, order_items, shipments, refunds)

    def order(self, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None
        return self.orders.get(order_id)

    def customer(self, customer_id: str | None) -> dict[str, Any] | None:
        if not customer_id:
            return None
        return self.customers.get(customer_id)

    def customer_by_email(self, email: str | None) -> dict[str, Any] | None:
        if not email:
            return None
        return self.customers_by_email.get(email)

    def lines(self, order_id: str | None) -> list[dict[str, Any]]:
        if not order_id:
            return []
        return self.lines_by_order.get(order_id, [])

    def shipment(self, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None
        return self.shipment_by_order.get(order_id)

    def refunded_amount(self, order_id: str, line_no: int) -> float:
        return round(
            sum(
                float(refund["amount_usd"])
                for refund in self.refunds_by_line.get((order_id, int(line_no)), [])
                if refund["status"] == "ISSUED"
            ),
            2,
        )

    def refunds_for_order(self, order_id: str | None) -> list[dict[str, Any]]:
        if not order_id:
            return []
        return [
            refund for refund in self.refunds_by_order.get(order_id, []) if refund["status"] == "ISSUED"
        ]
