"""Task 1 - reproducible data preparation.

One command::

    python -m src.prepare

Reads the raw data room (never modifies it), builds ``build/prepared.db`` with
cleaned + indexed tables, and writes ``build/prep_report.json`` containing the
exact row counts, every fix applied and every flag raised.

The command is idempotent: it always rebuilds into a temporary file and swaps
it in, and the report is deterministic (no wall-clock timestamps).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.normalize import (
    extract_original_sender,
    is_forward_subject,
    normalize_email,
    parse_date_utc,
    parse_money_usd,
    strip_forward_prefix,
    ticket_dedupe_key,
)

SCHEMA_VERSION = 1
DATA_FILES = (
    ("customers", "data/customers.csv"),
    ("orders", "data/orders.csv"),
    ("order_items", "data/order_items.csv"),
    ("shipments", "data/shipments.csv"),
    ("refunds", "data/refunds.csv"),
)
TICKET_FILES = (
    ("tickets", "tickets/support_inbox_export.json"),
    ("public_tickets", "inbox/public_tickets.json"),
    ("eval_labels", "tickets/eval_seed_labelled.json"),
)
SHIPMENT_STATUSES = {"DELIVERED", "IN_TRANSIT", "UNAVAILABLE"}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email_raw TEXT NOT NULL,
    email_norm TEXT NOT NULL,
    country TEXT,
    created_at_utc TEXT,
    loyalty_tier TEXT
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_date_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    order_total_usd REAL NOT NULL,
    shipping_fee_usd REAL NOT NULL,
    currency TEXT,
    payment_method TEXT,
    data_quality_status TEXT NOT NULL CHECK (data_quality_status IN ('OK', 'AMBIGUOUS')),
    ambiguous_fields TEXT,
    source_rows INTEGER NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE order_items (
    order_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_price_usd REAL NOT NULL,
    line_value_usd REAL NOT NULL,
    PRIMARY KEY (order_id, line_no),
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE shipments (
    shipment_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    carrier TEXT,
    tracking_no TEXT,
    status TEXT NOT NULL,
    shipped_at_utc TEXT,
    delivered_at_utc TEXT,
    last_event_at_utc TEXT,
    carrier_status_usable INTEGER NOT NULL CHECK (carrier_status_usable IN (0, 1)),
    movement_check_usable INTEGER NOT NULL CHECK (movement_check_usable IN (0, 1)),
    flag_codes TEXT NOT NULL,
    flag_detail TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE refunds (
    refund_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    amount_usd REAL NOT NULL,
    status TEXT NOT NULL,
    requested_at_utc TEXT,
    approved_by TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE tickets (
    ticket_id TEXT PRIMARY KEY,
    received_at_utc TEXT NOT NULL,
    channel TEXT,
    from_raw TEXT,
    from_norm TEXT NOT NULL,
    to_addr TEXT,
    subject TEXT,
    body TEXT,
    is_forward INTEGER NOT NULL CHECK (is_forward IN (0, 1)),
    original_subject TEXT,
    original_from TEXT NOT NULL,
    original_from_source TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL CHECK (is_duplicate IN (0, 1)),
    duplicate_of_ticket_id TEXT,
    actionable INTEGER NOT NULL CHECK (actionable IN (0, 1)),
    attachments_json TEXT NOT NULL
);

CREATE TABLE public_tickets (
    ticket_id TEXT PRIMARY KEY,
    received_at_utc TEXT NOT NULL,
    channel TEXT,
    from_raw TEXT,
    from_norm TEXT NOT NULL,
    to_addr TEXT,
    subject TEXT,
    body TEXT,
    is_forward INTEGER NOT NULL CHECK (is_forward IN (0, 1)),
    original_subject TEXT,
    original_from TEXT NOT NULL,
    original_from_source TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL CHECK (is_duplicate IN (0, 1)),
    duplicate_of_ticket_id TEXT,
    actionable INTEGER NOT NULL CHECK (actionable IN (0, 1)),
    attachments_json TEXT NOT NULL
);

CREATE TABLE ticket_attachments (
    ticket_id TEXT NOT NULL,
    source TEXT NOT NULL,
    attachment_index INTEGER NOT NULL,
    file_name TEXT,
    content_type TEXT,
    content_base64 TEXT,
    PRIMARY KEY (ticket_id, source, attachment_index)
);

CREATE TABLE eval_labels (
    ticket_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);

CREATE TABLE fixes (
    fix_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE flags (
    flag_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX idx_customers_email ON customers(email_norm);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_quality ON orders(data_quality_status);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_sku ON order_items(sku);
CREATE INDEX idx_shipments_order ON shipments(order_id);
CREATE INDEX idx_shipments_usable ON shipments(carrier_status_usable, movement_check_usable);
CREATE INDEX idx_refunds_order_line ON refunds(order_id, line_no);
CREATE INDEX idx_tickets_dedupe ON tickets(dedupe_key);
CREATE INDEX idx_tickets_actionable ON tickets(actionable);
CREATE INDEX idx_tickets_from ON tickets(from_norm);
CREATE INDEX idx_public_tickets_dedupe ON public_tickets(dedupe_key);
CREATE INDEX idx_flags_entity ON flags(entity_type, entity_id);

CREATE VIEW v_orders_checked AS
SELECT o.*,
       CASE WHEN o.data_quality_status = 'AMBIGUOUS' THEN 'UNABLE_TO_VERIFY' ELSE 'OK' END AS order_check_status
FROM orders o;

CREATE VIEW v_shipments_checked AS
SELECT s.*,
       CASE WHEN s.carrier_status_usable = 0 THEN 'UNABLE_TO_VERIFY' ELSE 'OK' END AS carrier_status_check,
       CASE WHEN s.movement_check_usable = 0 THEN 'UNABLE_TO_VERIFY' ELSE 'OK' END AS movement_check
FROM shipments s;

CREATE VIEW v_issued_refunds_by_line AS
SELECT order_id, line_no, SUM(amount_usd) AS refunded_usd, COUNT(*) AS refund_count
FROM refunds
WHERE status = 'ISSUED'
GROUP BY order_id, line_no;

CREATE VIEW v_tickets_actionable AS
SELECT * FROM tickets WHERE actionable = 1;
"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build build/prepared.db and build/prep_report.json.")
    parser.add_argument("--data-room", help="Data-room root containing data/, tickets/ and inbox/.")
    parser.add_argument("--out-dir", default="build", help="Output directory (default: build).")
    return parser.parse_args(argv)


def resolve_data_room(value: str | None) -> Path:
    here = Path(__file__).resolve()
    repo = here.parents[1]
    candidates = [Path(value)] if value else [Path.cwd(), repo, repo.parent]
    for candidate in candidates:
        room = candidate.expanduser().resolve()
        if (room / "data" / "orders.csv").is_file() and (room / "tickets" / "support_inbox_export.json").is_file():
            return room
    looked = ", ".join(str(p) for p in candidates)
    raise SystemExit(f"Could not find the data room (looked in: {looked}). Use --data-room.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_to_room(path: Path, room: Path) -> str:
    try:
        return path.resolve().relative_to(room.resolve()).as_posix()
    except ValueError:
        return path.name


def relative_to_repo(path: Path) -> str:
    repo = Path(__file__).resolve().parents[1]
    try:
        return Path(os.path.relpath(path.resolve(), repo)).as_posix()
    except ValueError:
        return path.name


def read_csv_rows(path: Path, room: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames or []
    info = {
        "path": relative_to_room(path, room),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "columns": columns,
        "rows_in": len(rows),
    }
    return rows, info


def read_json(path: Path, room: Path) -> tuple[Any, dict[str, Any]]:
    raw_bytes = path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8-sig"))
    info = {
        "path": relative_to_room(path, room),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "bytes": len(raw_bytes),
        "rows_in": len(data) if isinstance(data, list) else 1,
    }
    return data, info


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def money_key(table: str, field: str) -> str:
    return f"{table}.{field}"


def note_money(stats: defaultdict[str, Counter], table: str, field: str, parsed) -> None:
    stats[money_key(table, field)][parsed.rule] += 1


def note_date(stats: defaultdict[str, Counter], table: str, field: str, parsed) -> None:
    stats[money_key(table, field)][parsed.rule] += 1


def parse_optional_date(raw: str | None, table: str, field: str, row_ref: str,
                        date_stats: defaultdict[str, Counter],
                        ambiguous_dates: list[dict[str, Any]]) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    parsed = parse_date_utc(raw)
    note_date(date_stats, table, field, parsed)
    if parsed.ambiguous:
        ambiguous_dates.append(
            {
                "table": table,
                "field": field,
                "row_ref": row_ref,
                "raw": parsed.raw,
                "rule_applied": parsed.rule,
                "candidates": list(parsed.candidates),
                "resolved": parsed.value,
            }
        )
    return parsed.value


def parse_required_date(raw: str | None, table: str, field: str, row_ref: str,
                        date_stats: defaultdict[str, Counter],
                        ambiguous_dates: list[dict[str, Any]]) -> str:
    parsed = parse_optional_date(raw, table, field, row_ref, date_stats, ambiguous_dates)
    if parsed is None:
        raise ValueError(f"{table}.{field} is blank for {row_ref}")
    return parsed


def add_fix(report: dict[str, Any], code: str, entity_type: str, entity_id: str,
            detail: str) -> None:
    report["fixes_applied"].append(
        {
            "fix_id": f"FIX-{len(report['fixes_applied']) + 1:03d}",
            "code": code,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail,
        }
    )


def add_flag(report: dict[str, Any], code: str, entity_type: str, entity_id: str,
             detail: str) -> None:
    report["flags_raised"].append(
        {
            "flag_id": f"FLAG-{len(report['flags_raised']) + 1:03d}",
            "code": code,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail,
        }
    )


def new_report(data_room: Path, out_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "deterministic": True,
        "data_room": relative_to_repo(data_room),
        "outputs": {
            "database": relative_to_repo(out_dir / "prepared.db"),
            "prep_report": relative_to_repo(out_dir / "prep_report.json"),
        },
        "inputs": {"files": []},
        "counts": {},
        "normalization": {
            "email": {
                "function": "src.normalize.normalize_email",
                "rule": "trim outer whitespace + lowercase",
                "values_processed": 0,
                "values_changed": 0,
                "changed_by_source": {},
            },
            "money": {"output": "numeric USD, 2 decimals", "values_processed": 0, "rules": {}},
            "dates": {
                "output": "ISO-8601 UTC",
                "values_processed": 0,
                "rules": {},
                "slash_date_rule": "day-first DD/MM/YYYY (proven by 26/08/2026 and 30/09/2026)",
                "ambiguous_day_month": [],
            },
        },
        "fixes_applied": [],
        "flags_raised": [],
        "duplicate_orders": [],
        "duplicate_tickets": [],
        "forwarded_tickets": [],
        "shipment_flags": [],
        "ambiguous_orders": [],
        "data_gaps": [],
        "referential_integrity": {},
        "verification": {},
    }


def prepare_customers(rows: list[dict[str, str]], report: dict[str, Any],
                      date_stats: defaultdict[str, Counter],
                      ambiguous_dates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    email = report["normalization"]["email"]
    for raw in rows:
        customer_id = raw["customer_id"].strip()
        email_raw = raw["email"]
        email_norm = normalize_email(email_raw)
        email["values_processed"] += 1
        if email_norm != email_raw:
            email["values_changed"] += 1
            email["changed_by_source"]["customers"] = email["changed_by_source"].get("customers", 0) + 1
            add_fix(report, "NORMALIZE_EMAIL", "customers", customer_id,
                    f"{email_raw!r} -> {email_norm!r}")
        records.append(
            {
                "customer_id": customer_id,
                "name": raw["name"].strip(),
                "email_raw": email_raw,
                "email_norm": email_norm,
                "country": raw["country"].strip(),
                "created_at_utc": parse_required_date(
                    raw["created_at"], "customers", "created_at", customer_id, date_stats, ambiguous_dates
                ),
                "loyalty_tier": raw["loyalty_tier"].strip(),
            }
        )
    return sorted(records, key=lambda item: item["customer_id"])


def prepare_orders(rows: list[dict[str, str]], report: dict[str, Any],
                   money_stats: defaultdict[str, Counter],
                   date_stats: defaultdict[str, Counter],
                   ambiguous_dates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, raw in enumerate(rows):
        order_id = raw["order_id"].strip()
        total = parse_money_usd(raw["Order Total"])
        shipping = parse_money_usd(raw["shipping_fee"])
        note_money(money_stats, "orders", "Order Total", total)
        note_money(money_stats, "orders", "shipping_fee", shipping)
        record = {
            "order_id": order_id,
            "customer_id": raw["customer_id"].strip(),
            "order_date_utc": parse_required_date(
                raw["order_date"], "orders", "order_date", order_id, date_stats, ambiguous_dates
            ),
            "status": raw["status"].strip(),
            "order_total_usd": float(total.amount),
            "shipping_fee_usd": float(shipping.amount),
            "currency": raw["currency"].strip(),
            "payment_method": raw["payment_method"].strip(),
        }
        grouped[order_id].append((index, record))

    records: list[dict[str, Any]] = []
    compare_fields = (
        "customer_id",
        "order_date_utc",
        "status",
        "order_total_usd",
        "shipping_fee_usd",
        "currency",
        "payment_method",
    )
    for order_id in sorted(grouped):
        group = grouped[order_id]
        canonical = group[0][1]
        distinct = {tuple(item.get(field) for field in compare_fields) for _, item in group}
        conflict = len(distinct) > 1
        duplicate_group = len(group) > 1
        ambiguous_fields: list[str] = []
        if conflict:
            for field in compare_fields:
                if len({item.get(field) for _, item in group}) > 1:
                    ambiguous_fields.append(field)
            add_flag(
                report,
                "ORDER_AMBIGUOUS",
                "orders",
                order_id,
                f"{len(group)} rows conflict on {', '.join(ambiguous_fields)}; checks must return UNABLE_TO_VERIFY",
            )
            report["ambiguous_orders"].append(
                {
                    "order_id": order_id,
                    "source_rows": len(group),
                    "conflicting_fields": ambiguous_fields,
                }
            )
        elif duplicate_group:
            add_fix(
                report,
                "DUPLICATE_ORDER_DEDUPED",
                "orders",
                order_id,
                f"{len(group)} identical rows reduced to 1",
            )
        if duplicate_group:
            report["duplicate_orders"].append(
                {
                    "order_id": order_id,
                    "source_rows": len(group),
                    "resolution": "AMBIGUOUS" if conflict else "DEDUPED",
                    "conflicting_fields": ambiguous_fields,
                }
            )
        record = dict(canonical)
        record["data_quality_status"] = "AMBIGUOUS" if conflict else "OK"
        record["ambiguous_fields"] = json.dumps(ambiguous_fields) if conflict else None
        record["source_rows"] = len(group)
        records.append(record)
    return records


def prepare_order_items(rows: list[dict[str, str]], money_stats: defaultdict[str, Counter]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in rows:
        order_id = raw["order_id"].strip()
        line_no = int(raw["line_no"])
        key = (order_id, line_no)
        if key in seen:
            raise ValueError(f"duplicate order line {key}")
        seen.add(key)
        unit = parse_money_usd(raw["unit_price"])
        note_money(money_stats, "order_items", "unit_price", unit)
        qty = int(raw["qty"])
        if qty <= 0:
            raise ValueError(f"non-positive qty for {key}")
        line_value = (unit.amount * qty).quantize(Decimal("0.01"))
        records.append(
            {
                "order_id": order_id,
                "line_no": line_no,
                "sku": raw["sku"].strip(),
                "product_name": raw["product_name"].strip(),
                "qty": qty,
                "unit_price_usd": float(unit.amount),
                "line_value_usd": float(line_value),
            }
        )
    return sorted(records, key=lambda item: (item["order_id"], item["line_no"]))


def prepare_shipments(rows: list[dict[str, str]], report: dict[str, Any],
                      date_stats: defaultdict[str, Counter],
                      ambiguous_dates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in rows:
        shipment_id = raw["shipment_id"].strip()
        order_id = raw["order_id"].strip()
        status = raw["status"].strip()
        shipped = parse_optional_date(raw["shipped_at"], "shipments", "shipped_at", shipment_id, date_stats, ambiguous_dates)
        delivered = parse_optional_date(raw["delivered_at"], "shipments", "delivered_at", shipment_id, date_stats, ambiguous_dates)
        last_event = parse_optional_date(raw["last_event_at"], "shipments", "last_event_at", shipment_id, date_stats, ambiguous_dates)

        codes: list[str] = []
        status_usable = 1
        movement_usable = 1

        if status not in SHIPMENT_STATUSES or status == "UNAVAILABLE":
            codes.append("CARRIER_STATUS_UNAVAILABLE")
            status_usable = 0
            movement_usable = 0
        if status == "DELIVERED" and delivered is None:
            codes.append("DELIVERED_WITHOUT_SCAN")
            movement_usable = 0
        if shipped and last_event and last_event < shipped:
            codes.append("LAST_EVENT_BEFORE_SHIPPED")
            movement_usable = 0
        if status == "IN_TRANSIT" and last_event is None:
            codes.append("MISSING_LAST_EVENT")
            movement_usable = 0

        detail = {
            "status": status,
            "shipped_at": shipped,
            "delivered_at": delivered,
            "last_event_at": last_event,
        }
        record = {
            "shipment_id": shipment_id,
            "order_id": order_id,
            "carrier": raw["carrier"].strip(),
            "tracking_no": raw["tracking_no"].strip(),
            "status": status,
            "shipped_at_utc": shipped,
            "delivered_at_utc": delivered,
            "last_event_at_utc": last_event,
            "carrier_status_usable": status_usable,
            "movement_check_usable": movement_usable,
            "flag_codes": json.dumps(codes),
            "flag_detail": json.dumps(detail, sort_keys=True),
        }
        records.append(record)

        if codes:
            add_flag(
                report,
                "SHIPMENT_UNVERIFIABLE",
                "shipments",
                shipment_id,
                f"order {order_id}: {', '.join(codes)} (status={status}, shipped={shipped}, last_event={last_event})",
            )
            report["shipment_flags"].append(
                {
                    "shipment_id": shipment_id,
                    "order_id": order_id,
                    "codes": codes,
                    "status": status,
                    "shipped_at_utc": shipped,
                    "last_event_at_utc": last_event,
                    "carrier_status_usable": bool(status_usable),
                    "movement_check_usable": bool(movement_usable),
                }
            )
    return sorted(records, key=lambda item: item["shipment_id"])


def prepare_refunds(rows: list[dict[str, str]], money_stats: defaultdict[str, Counter],
                    date_stats: defaultdict[str, Counter],
                    ambiguous_dates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in rows:
        refund_id = raw["refund_id"].strip()
        amount = parse_money_usd(raw["amount"])
        note_money(money_stats, "refunds", "amount", amount)
        records.append(
            {
                "refund_id": refund_id,
                "order_id": raw["order_id"].strip(),
                "line_no": int(raw["line_no"]),
                "amount_usd": float(amount.amount),
                "status": raw["status"].strip(),
                "requested_at_utc": parse_optional_date(
                    raw["requested_at"], "refunds", "requested_at", refund_id, date_stats, ambiguous_dates
                ),
                "approved_by": (raw.get("approved_by") or "").strip() or None,
            }
        )
    return sorted(records, key=lambda item: item["refund_id"])


def prepare_ticket_records(raw_tickets: list[dict[str, Any]], source: str,
                           report: dict[str, Any],
                           date_stats: defaultdict[str, Counter],
                           ambiguous_dates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    email = report["normalization"]["email"]
    for raw in raw_tickets:
        ticket_id = str(raw["ticket_id"]).strip()
        email_raw = raw.get("from", "")
        email_norm = normalize_email(email_raw)
        email["values_processed"] += 1
        if email_norm != email_raw:
            email["values_changed"] += 1
            email["changed_by_source"][source] = email["changed_by_source"].get(source, 0) + 1
            add_fix(report, "NORMALIZE_EMAIL", source, ticket_id, f"{email_raw!r} -> {email_norm!r}")

        subject = raw.get("subject", "")
        body = raw.get("body", "")
        forwarded = is_forward_subject(subject)
        original_subject = strip_forward_prefix(subject)
        if forwarded:
            original_from, original_from_source = extract_original_sender(body, email_norm)
            report["forwarded_tickets"].append(
                {
                    "source": source,
                    "ticket_id": ticket_id,
                    "original_from": original_from,
                    "original_from_source": original_from_source,
                    "dedupe_key": ticket_dedupe_key(original_from, original_subject, body),
                }
            )
        else:
            original_from = email_norm
            original_from_source = "not_forwarded"

        attachments = raw.get("attachments") or []
        records.append(
            {
                "ticket_id": ticket_id,
                "received_at_utc": parse_required_date(
                    raw.get("received_at"), source, "received_at", ticket_id, date_stats, ambiguous_dates
                ),
                "channel": raw.get("channel"),
                "from_raw": email_raw,
                "from_norm": email_norm,
                "to_addr": raw.get("to"),
                "subject": subject,
                "body": body,
                "is_forward": 1 if forwarded else 0,
                "original_subject": original_subject,
                "original_from": original_from,
                "original_from_source": original_from_source,
                "dedupe_key": ticket_dedupe_key(original_from, original_subject, body),
                "is_duplicate": 0,
                "duplicate_of_ticket_id": None,
                "actionable": 1,
                "attachments": attachments,
            }
        )

    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["dedupe_key"]].append(record)

    duplicates: list[dict[str, Any]] = []
    for dedupe_key in sorted(groups):
        group = groups[dedupe_key]
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: (item["is_forward"], item["received_at_utc"], item["ticket_id"]))
        canonical = ordered[0]
        for duplicate in ordered[1:]:
            duplicate["is_duplicate"] = 1
            duplicate["duplicate_of_ticket_id"] = canonical["ticket_id"]
            duplicate["actionable"] = 0
            duplicates.append(
                {
                    "source": source,
                    "ticket_id": duplicate["ticket_id"],
                    "duplicate_of_ticket_id": canonical["ticket_id"],
                    "is_forward": bool(duplicate["is_forward"]),
                    "original_from": duplicate["original_from"],
                    "original_from_source": duplicate["original_from_source"],
                    "dedupe_key": dedupe_key,
                }
            )
            add_fix(
                report,
                "DUPLICATE_TICKET_NON_ACTIONABLE",
                source,
                duplicate["ticket_id"],
                f"duplicate of {canonical['ticket_id']}; dedupe_key={dedupe_key}; actionable=0",
            )
            add_flag(
                report,
                "DUPLICATE_TICKET",
                source,
                duplicate["ticket_id"],
                f"duplicate of {canonical['ticket_id']}; must not produce a second action",
            )

    return sorted(records, key=lambda item: item["ticket_id"]), duplicates


def build_database(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        meta_rows = [("schema_version", str(SCHEMA_VERSION)), ("generated_by", "src.prepare")]
        meta_rows.extend(
            (f"source_sha256:{item['path']}", item["sha256"]) for item in payload["source_hashes"]
        )
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta_rows)
        conn.executemany(
            "INSERT INTO customers VALUES (:customer_id, :name, :email_raw, :email_norm, :country, :created_at_utc, :loyalty_tier)",
            payload["customers"],
        )
        conn.executemany(
            """INSERT INTO orders VALUES (
                :order_id, :customer_id, :order_date_utc, :status, :order_total_usd,
                :shipping_fee_usd, :currency, :payment_method, :data_quality_status,
                :ambiguous_fields, :source_rows
            )""",
            payload["orders"],
        )
        conn.executemany(
            """INSERT INTO order_items VALUES (
                :order_id, :line_no, :sku, :product_name, :qty, :unit_price_usd, :line_value_usd
            )""",
            payload["order_items"],
        )
        conn.executemany(
            """INSERT INTO shipments VALUES (
                :shipment_id, :order_id, :carrier, :tracking_no, :status, :shipped_at_utc,
                :delivered_at_utc, :last_event_at_utc, :carrier_status_usable,
                :movement_check_usable, :flag_codes, :flag_detail
            )""",
            payload["shipments"],
        )
        conn.executemany(
            "INSERT INTO refunds VALUES (:refund_id, :order_id, :line_no, :amount_usd, :status, :requested_at_utc, :approved_by)",
            payload["refunds"],
        )
        ticket_sql = """INSERT INTO {table} VALUES (
            :ticket_id, :received_at_utc, :channel, :from_raw, :from_norm, :to_addr,
            :subject, :body, :is_forward, :original_subject, :original_from,
            :original_from_source, :dedupe_key, :is_duplicate, :duplicate_of_ticket_id,
            :actionable, :attachments_json
        )"""
        ticket_rows = [
            {**record, "attachments_json": json.dumps(record["attachments"], sort_keys=True)}
            for record in payload["tickets"]
        ]
        public_ticket_rows = [
            {**record, "attachments_json": json.dumps(record["attachments"], sort_keys=True)}
            for record in payload["public_tickets"]
        ]
        conn.executemany(ticket_sql.format(table="tickets"), ticket_rows)
        conn.executemany(ticket_sql.format(table="public_tickets"), public_ticket_rows)
        conn.executemany(
            "INSERT INTO ticket_attachments VALUES (?, ?, ?, ?, ?, ?)",
            payload["attachments"],
        )
        conn.executemany(
            "INSERT INTO eval_labels VALUES (:ticket_id, :payload_json)",
            payload["eval_labels"],
        )
        conn.executemany(
            "INSERT INTO fixes VALUES (:fix_id, :code, :entity_type, :entity_id, :detail)",
            payload["fixes_applied"],
        )
        conn.executemany(
            "INSERT INTO flags VALUES (:flag_id, :code, :entity_type, :entity_id, :detail)",
            payload["flags_raised"],
        )
        conn.commit()
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    data_room = resolve_data_room(args.data_room)
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = new_report(data_room, out_dir)

    money_stats: defaultdict[str, Counter] = defaultdict(Counter)
    date_stats: defaultdict[str, Counter] = defaultdict(Counter)
    ambiguous_dates: list[dict[str, Any]] = []
    input_hashes: dict[Path, str] = {}

    raw: dict[str, list[dict[str, str]]] = {}
    for table, relative_path in DATA_FILES:
        path = data_room / relative_path
        rows, info = read_csv_rows(path, data_room)
        raw[table] = rows
        report["inputs"]["files"].append(info)
        input_hashes[path] = info["sha256"]

    ticket_raw: dict[str, Any] = {}
    for table, relative_path in TICKET_FILES:
        path = data_room / relative_path
        data, info = read_json(path, data_room)
        ticket_raw[table] = data
        report["inputs"]["files"].append(info)
        input_hashes[path] = info["sha256"]

    customers = prepare_customers(raw["customers"], report, date_stats, ambiguous_dates)
    orders = prepare_orders(raw["orders"], report, money_stats, date_stats, ambiguous_dates)
    order_items = prepare_order_items(raw["order_items"], money_stats)
    shipments = prepare_shipments(raw["shipments"], report, date_stats, ambiguous_dates)
    refunds = prepare_refunds(raw["refunds"], money_stats, date_stats, ambiguous_dates)

    tickets, ticket_duplicates = prepare_ticket_records(
        ticket_raw["tickets"], "tickets", report, date_stats, ambiguous_dates
    )
    report["duplicate_tickets"].extend(ticket_duplicates)
    public_tickets, public_duplicates = prepare_ticket_records(
        ticket_raw["public_tickets"], "public_tickets", report, date_stats, ambiguous_dates
    )
    report["duplicate_tickets"].extend(public_duplicates)

    eval_labels = [
        {"ticket_id": item["ticket_id"], "payload_json": json.dumps(item, sort_keys=True)}
        for item in sorted(ticket_raw["eval_labels"], key=lambda item: item["ticket_id"])
    ]

    attachments: list[tuple[str, str, int, str | None, str | None, str | None]] = []
    for source, records in (("tickets", tickets), ("public_tickets", public_tickets)):
        for record in records:
            for index, attachment in enumerate(record["attachments"]):
                attachments.append(
                    (
                        record["ticket_id"],
                        source,
                        index,
                        attachment.get("file_name"),
                        attachment.get("content_type"),
                        attachment.get("content_base64"),
                    )
                )

    report["normalization"]["money"]["values_processed"] = sum(
        sum(counter.values()) for counter in money_stats.values()
    )
    report["normalization"]["money"]["rules"] = {
        key: dict(sorted(counter.items())) for key, counter in sorted(money_stats.items())
    }
    report["normalization"]["dates"]["values_processed"] = sum(
        sum(counter.values()) for counter in date_stats.values()
    )
    report["normalization"]["dates"]["rules"] = {
        key: dict(sorted(counter.items())) for key, counter in sorted(date_stats.items())
    }
    report["normalization"]["dates"]["ambiguous_day_month"] = sorted(
        ambiguous_dates,
        key=lambda item: (item["table"], item["field"], item["row_ref"]),
    )
    report["normalization"]["email"]["values_processed"] = (
        len(customers) + len(tickets) + len(public_tickets)
    )
    report["normalization"]["email"]["changed_by_source"] = dict(
        sorted(report["normalization"]["email"]["changed_by_source"].items())
    )

    add_fix(
        report,
        "PARSE_MONEY_USD",
        "orders",
        "*",
        f"{report['normalization']['money']['values_processed']} money values parsed to numeric USD using $/USD/plain rules",
    )
    add_fix(
        report,
        "PARSE_DATES_UTC",
        "all",
        "*",
        f"{report['normalization']['dates']['values_processed']} dates parsed to ISO-8601 UTC; "
        f"{len(ambiguous_dates)} ambiguous day/month values resolved day-first",
    )

    report["counts"] = {
        "customers": {"rows_in": len(raw["customers"]), "rows_out": len(customers)},
        "orders": {"rows_in": len(raw["orders"]), "rows_out": len(orders)},
        "order_items": {"rows_in": len(raw["order_items"]), "rows_out": len(order_items)},
        "shipments": {"rows_in": len(raw["shipments"]), "rows_out": len(shipments)},
        "refunds": {"rows_in": len(raw["refunds"]), "rows_out": len(refunds)},
        "tickets": {
            "rows_in": len(ticket_raw["tickets"]),
            "rows_out": len(tickets),
            "actionable_out": sum(record["actionable"] for record in tickets),
            "duplicates_out": sum(record["is_duplicate"] for record in tickets),
            "attachments_out": sum(len(record["attachments"]) for record in tickets),
        },
        "public_tickets": {
            "rows_in": len(ticket_raw["public_tickets"]),
            "rows_out": len(public_tickets),
            "actionable_out": sum(record["actionable"] for record in public_tickets),
            "duplicates_out": sum(record["is_duplicate"] for record in public_tickets),
            "attachments_out": sum(len(record["attachments"]) for record in public_tickets),
        },
        "eval_labels": {"rows_in": len(ticket_raw["eval_labels"]), "rows_out": len(eval_labels)},
        "fixes": len(report["fixes_applied"]),
        "flags": len(report["flags_raised"]),
        "duplicate_orders": len(report["duplicate_orders"]),
        "duplicate_tickets": len(report["duplicate_tickets"]),
        "forwarded_tickets": len(report["forwarded_tickets"]),
        "shipment_flags": len(report["shipment_flags"]),
        "ambiguous_dates": len(ambiguous_dates),
        "ambiguous_orders": len(report["ambiguous_orders"]),
        "attachments": len(attachments),
        "flag_codes": dict(sorted(Counter(flag["code"] for flag in report["flags_raised"]).items())),
        "shipment_flag_codes": dict(
            sorted(Counter(code for item in report["shipment_flags"] for code in item["codes"]).items())
        ),
    }
    report["counts"]["removed_rows"] = {
        "orders": len(raw["orders"]) - len(orders),
        "tickets_actionable": len(tickets) - report["counts"]["tickets"]["actionable_out"],
    }

    order_ids = {order["order_id"] for order in orders}
    customer_ids = {customer["customer_id"] for customer in customers}
    item_keys = {(item["order_id"], item["line_no"]) for item in order_items}
    report["referential_integrity"] = {
        "items_with_unknown_order": sorted({item["order_id"] for item in order_items} - order_ids),
        "shipments_with_unknown_order": sorted({shipment["order_id"] for shipment in shipments} - order_ids),
        "refunds_with_unknown_order": sorted({refund["order_id"] for refund in refunds} - order_ids),
        "refunds_with_unknown_line": sorted(
            {
                f"{refund['order_id']}:{refund['line_no']}"
                for refund in refunds
                if (refund["order_id"], refund["line_no"]) not in item_keys
            }
        ),
        "orders_with_unknown_customer": sorted({order["customer_id"] for order in orders} - customer_ids),
        "orders_without_items": sorted(order_ids - {item["order_id"] for item in order_items}),
        "orders_without_shipments": sorted(order_ids - {shipment["order_id"] for shipment in shipments}),
    }

    numeric_ids = sorted(int(order_id.split("-")[1]) for order_id in order_ids)
    present = set(numeric_ids)
    missing_ids = [f"LF-{value:06d}" for value in range(numeric_ids[0], numeric_ids[-1] + 1) if value not in present]
    report["data_gaps"] = [
        {
            "code": "ORDER_ID_GAP",
            "order_id": order_id,
            "detail": "absent from orders, order_items and shipments; a request for it must return ORDER_NOT_FOUND",
        }
        for order_id in missing_ids
    ]

    payload = {
        "customers": customers,
        "orders": orders,
        "order_items": order_items,
        "shipments": shipments,
        "refunds": refunds,
        "tickets": tickets,
        "public_tickets": public_tickets,
        "eval_labels": eval_labels,
        "attachments": attachments,
        "fixes_applied": report["fixes_applied"],
        "flags_raised": report["flags_raised"],
        "source_hashes": report["inputs"]["files"],
    }

    db_path = out_dir / "prepared.db"
    tmp_db = out_dir / f".prepared.db.{os.getpid()}.tmp"
    if tmp_db.exists():
        tmp_db.unlink()
    try:
        build_database(tmp_db, payload)
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            table_counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "customers",
                    "orders",
                    "order_items",
                    "shipments",
                    "refunds",
                    "tickets",
                    "public_tickets",
                    "eval_labels",
                    "fixes",
                    "flags",
                )
            }
            actionable_duplicates = conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE is_duplicate = 1 AND actionable = 1"
            ).fetchone()[0]
            bad_carrier_flags = conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE carrier_status_usable = 0 AND flag_codes NOT LIKE '%CARRIER_STATUS_UNAVAILABLE%'"
            ).fetchone()[0]
            bad_movement_flags = conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE movement_check_usable = 0 AND flag_codes = '[]'"
            ).fetchone()[0]
        finally:
            conn.close()
        os.replace(tmp_db, db_path)
    finally:
        if tmp_db.exists():
            tmp_db.unlink()

    raw_untouched = True
    for path, expected in input_hashes.items():
        if sha256_file(path) != expected:
            raw_untouched = False

    report["verification"] = {
        "raw_files_untouched": raw_untouched,
        "sqlite_integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "duplicate_tickets_actionable": actionable_duplicates,
        "carrier_flags_inconsistent": bad_carrier_flags,
        "movement_flags_inconsistent": bad_movement_flags,
        "table_counts": table_counts,
        "counts_match": (
            table_counts["customers"] == report["counts"]["customers"]["rows_out"]
            and table_counts["orders"] == report["counts"]["orders"]["rows_out"]
            and table_counts["order_items"] == report["counts"]["order_items"]["rows_out"]
            and table_counts["shipments"] == report["counts"]["shipments"]["rows_out"]
            and table_counts["refunds"] == report["counts"]["refunds"]["rows_out"]
            and table_counts["tickets"] == report["counts"]["tickets"]["rows_out"]
            and table_counts["public_tickets"] == report["counts"]["public_tickets"]["rows_out"]
            and table_counts["eval_labels"] == report["counts"]["eval_labels"]["rows_out"]
        ),
    }
    if not all(
        (
            report["verification"]["raw_files_untouched"],
            report["verification"]["sqlite_integrity_check"] == "ok",
            report["verification"]["foreign_key_violations"] == 0,
            report["verification"]["duplicate_tickets_actionable"] == 0,
            report["verification"]["carrier_flags_inconsistent"] == 0,
            report["verification"]["movement_flags_inconsistent"] == 0,
            report["verification"]["counts_match"],
        )
    ):
        raise SystemExit("prepare verification failed; see build/prep_report.json")

    atomic_write_json(out_dir / "prep_report.json", report)

    print(
        "prepare: customers {customers} | orders {orders_in}->{orders_out} | items {items} | "
        "shipments {shipments} ({shipment_flags} flagged) | refunds {refunds}".format(
            customers=report["counts"]["customers"]["rows_out"],
            orders_in=report["counts"]["orders"]["rows_in"],
            orders_out=report["counts"]["orders"]["rows_out"],
            items=report["counts"]["order_items"]["rows_out"],
            shipments=report["counts"]["shipments"]["rows_out"],
            shipment_flags=report["counts"]["shipment_flags"],
            refunds=report["counts"]["refunds"]["rows_out"],
        )
    )
    print(
        "prepare: tickets {tin}->{tout} ({dup} duplicates non-actionable) | public {pin} | labels {labels}".format(
            tin=report["counts"]["tickets"]["rows_in"],
            tout=report["counts"]["tickets"]["rows_out"],
            dup=report["counts"]["duplicate_tickets"],
            pin=report["counts"]["public_tickets"]["rows_out"],
            labels=report["counts"]["eval_labels"]["rows_out"],
        )
    )
    print(f"prepare: wrote {db_path} and {out_dir / 'prep_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
