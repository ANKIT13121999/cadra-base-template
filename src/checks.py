"""Pure-Python policy checks and decision logic (no LLM, no network).

Every check maps to one clause in ``policies/refund_policy_RP-3.2.md`` and
returns the contract shape::

    {"check_id": str, "result": PASS|FAIL|NOT_APPLICABLE|UNABLE_TO_VERIFY,
     "policy_clause": str, "evidence": [{"source", "ref", "value"}, ...]}

A ``details`` key is added for the decision engine. Missing or data-quality
flagged evidence never produces PASS.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable, Iterable

from src.normalize import normalize_email, normalize_order_ref, normalize_text, parse_date_utc
from src.repo import PreparedData

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNABLE_TO_VERIFY = "UNABLE_TO_VERIFY"

VALID_RESULTS = frozenset({PASS, FAIL, NOT_APPLICABLE, UNABLE_TO_VERIFY})
REFUND_TYPES = {"REFUND", "RETURN"}
TICKET_TYPES = {"REFUND", "RETURN", "ORDER_STATUS", "DELIVERY_ISSUE", "OTHER"}

# Reason codes named by RP-3.2 plus engine-only codes documented in
# docs/stated_rules.md. Engine-only codes are only used where the policy is
# silent about the data condition.
RC_OVERRIDE = "POLICY_OVERRIDE_ATTEMPT"
RC_ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
RC_CUSTOMER_MISMATCH = "CUSTOMER_MISMATCH"
RC_CARRIER_UNAVAILABLE = "CARRIER_STATUS_UNAVAILABLE"
RC_LOST = "LOST_IN_TRANSIT"
RC_OUT_OF_WINDOW = "OUT_OF_WINDOW"
RC_PARTIAL_LINES = "PARTIAL_LINES"
RC_ALREADY_REFUNDED = "ALREADY_REFUNDED"
RC_APPROVAL = "APPROVAL_REQUIRED"
RC_DAMAGE_EXCEPTION = "DAMAGE_EXCEPTION"
RC_MOVEMENT_UNVERIFIABLE = "MOVEMENT_DATA_UNVERIFIABLE"
RC_DELIVERY_DATE_UNAVAILABLE = "DELIVERY_DATE_UNAVAILABLE"
RC_AFFECTED_LINES_UNVERIFIABLE = "AFFECTED_LINE_UNVERIFIABLE"
RC_ORDER_AMBIGUOUS = "ORDER_AMBIGUOUS"
RC_IDENTITY_UNVERIFIABLE = "IDENTITY_UNVERIFIABLE"
RC_SCOPE_UNVERIFIABLE = "SCOPE_UNVERIFIABLE"
RC_ALREADY_PROCESSED = "ALREADY_PROCESSED"
RC_UNCLASSIFIED = "UNCLASSIFIED_REQUEST"
RC_SAFETY_UNVERIFIABLE = "OVERRIDE_CHECK_UNAVAILABLE"

AUTO_APPROVAL_LIMIT = 150.00
ORDINARY_WINDOW_DAYS = 30
DAMAGE_WINDOW_DAYS = 45
MOVEMENT_LOST_DAYS = 10.0


_MISSING = object()


def _get(facts: Any, name: str) -> Any:
    if isinstance(facts, Mapping):
        return facts.get(name, _MISSING)
    return getattr(facts, name, _MISSING)


def _bool_fact(facts: Any, name: str) -> bool | None:
    value = _get(facts, name)
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    return None


def _ticket_type(facts: Any) -> str | None:
    value = _get(facts, "ticket_type")
    if value is _MISSING or value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized if normalized in TICKET_TYPES else None


def _received_at(facts: Any) -> str | None:
    value = _get(facts, "received_at_utc")
    if value is _MISSING or value is None:
        value = _get(facts, "received_at")
    if value is _MISSING or value is None:
        return None
    try:
        return parse_date_utc(str(value)).value
    except ValueError:
        return None


def _sender_email(facts: Any) -> str | None:
    value = _get(facts, "customer_email")
    if value is _MISSING or value is None:
        value = _get(facts, "from_norm")
    if value is _MISSING or value is None:
        value = _get(facts, "from_email")
    if value is _MISSING or value is None:
        return None
    normalized = normalize_email(str(value))
    return normalized or None


def _order_refs(facts: Any) -> list[str]:
    value = _get(facts, "order_ref")
    if value is _MISSING or value is None:
        value = _get(facts, "order_id")
    if value is _MISSING or value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    refs = {normalize_order_ref(str(item)) for item in values}
    return sorted(ref for ref in refs if ref)


def _affected_items(facts: Any) -> list[dict[str, Any]] | None:
    value = _get(facts, "affected_items")
    if value is _MISSING or value is None:
        names = _get(facts, "product_names")
        if names is _MISSING or names is None:
            return None
        value = names
    if isinstance(value, str):
        value = [value]
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            name = item.get("product_name") or item.get("name")
            sku = item.get("sku")
            quantity = item.get("quantity", item.get("qty", 1))
        else:
            name = str(item)
            sku = None
            quantity = 1
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            quantity = 1
        items.append(
            {
                "product_name": str(name) if name else None,
                "sku": str(sku) if sku else None,
                "quantity": max(1, quantity),
            }
        )
    return items


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _evidence(source: str, ref: Any, value: Any) -> dict[str, str]:
    return {"source": source, "ref": str(ref), "value": str(value)}


def _check(
    check_id: str,
    result: str,
    policy_clause: str,
    evidence: Iterable[dict[str, str]],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if result not in VALID_RESULTS:
        raise ValueError(f"invalid check result {result!r}")
    payload: dict[str, Any] = {
        "check_id": check_id,
        "result": result,
        "policy_clause": policy_clause,
        "evidence": list(evidence),
    }
    if details is not None:
        payload["details"] = details
    return payload


def _not_applicable(check_id: str, clause: str, reason: str) -> dict[str, Any]:
    return _check(check_id, NOT_APPLICABLE, clause, [_evidence("ticket", "intent", reason)])


def _matches(item: dict[str, Any], line: dict[str, Any]) -> bool:
    sku = (item.get("sku") or "").strip().lower()
    if sku and str(line.get("sku", "")).strip().lower() == sku:
        return True
    name = normalize_text(item.get("product_name"))
    line_name = normalize_text(line.get("product_name"))
    if not name or not line_name:
        return False
    return name == line_name or name in line_name or line_name in name


def _calculate(facts: Any, data: PreparedData) -> dict[str, Any]:
    """Resolve affected order lines, line values, prior credit and unpaid value."""
    refs = _order_refs(facts)
    if len(refs) != 1:
        return {"error": RC_ORDER_AMBIGUOUS if len(refs) > 1 else RC_ORDER_NOT_FOUND}
    order = data.order(refs[0])
    if order is None:
        return {"error": RC_ORDER_NOT_FOUND}
    if order.get("data_quality_status") == "AMBIGUOUS":
        return {"error": RC_ORDER_AMBIGUOUS}

    items = _affected_items(facts)
    if items is None:
        return {"error": RC_AFFECTED_LINES_UNVERIFIABLE}
    if not items:
        return {"error": RC_AFFECTED_LINES_UNVERIFIABLE}

    lines = data.lines(order["order_id"])
    requested_by_line: dict[int, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for item in items:
        matches = [line for line in lines if _matches(item, line)]
        if not matches:
            unresolved.append(item)
            continue
        line = matches[0]
        entry = requested_by_line.setdefault(
            int(line["line_no"]),
            {"line": line, "requested_qty": 0, "items": []},
        )
        entry["requested_qty"] += int(item["quantity"])
        entry["items"].append(item.get("product_name") or item.get("sku") or "item")

    if unresolved:
        return {
            "error": RC_AFFECTED_LINES_UNVERIFIABLE,
            "unresolved_items": unresolved,
        }

    affected: list[dict[str, Any]] = []
    for line_no in sorted(requested_by_line):
        entry = requested_by_line[line_no]
        line = entry["line"]
        requested = int(entry["requested_qty"])
        ordered = int(line["qty"])
        quantity = min(requested, ordered)
        line_value = round(quantity * float(line["unit_price_usd"]), 2)
        refunded = data.refunded_amount(order["order_id"], line_no)
        unpaid = round(max(0.0, line_value - refunded), 2)
        affected.append(
            {
                "order_id": order["order_id"],
                "line_no": line_no,
                "sku": line["sku"],
                "product_name": line["product_name"],
                "matched_items": entry["items"],
                "ordered_qty": ordered,
                "affected_qty": quantity,
                "requested_qty": requested,
                "quantity_clamped": requested > ordered,
                "unit_price_usd": float(line["unit_price_usd"]),
                "line_value_usd": line_value,
                "refunded_usd": refunded,
                "unpaid_usd": unpaid,
            }
        )

    prior_refunds = [
        {
            "refund_id": refund["refund_id"],
            "line_no": int(refund["line_no"]),
            "amount_usd": float(refund["amount_usd"]),
            "status": refund["status"],
            "approved_by": refund.get("approved_by"),
        }
        for refund in data.refunds_for_order(order["order_id"])
    ]
    line_value_total = round(sum(item["line_value_usd"] for item in affected), 2)
    refundable_amount = round(sum(item["unpaid_usd"] for item in affected), 2)
    return {
        "order_id": order["order_id"],
        "order": order,
        "affected": affected,
        "line_value_total": line_value_total,
        "refundable_amount": refundable_amount,
        "has_prior_refund": bool(prior_refunds),
        "prior_refunds": prior_refunds,
        "all_affected_paid": bool(affected) and all(item["unpaid_usd"] <= 0 for item in affected),
    }


def _delivery_context(facts: Any, data: PreparedData) -> tuple[str, Any, dict[str, Any] | None]:
    refs = _order_refs(facts)
    if len(refs) != 1:
        return "ERROR", RC_ORDER_AMBIGUOUS if len(refs) > 1 else RC_ORDER_NOT_FOUND, None
    shipment = data.shipment(refs[0])
    if shipment is None:
        return "UNABLE", "NO_SHIPMENT", None
    if int(shipment.get("carrier_status_usable", 0)) == 0:
        return "UNABLE", RC_CARRIER_UNAVAILABLE, shipment
    if shipment.get("flag_codes"):
        return "UNABLE", "SHIPMENT_FLAGGED", shipment
    if shipment.get("status") != "DELIVERED":
        return "UNABLE", "NOT_DELIVERED", shipment
    received = _received_at(facts)
    delivered = shipment.get("delivered_at_utc")
    if not received or not delivered:
        return "UNABLE", "MISSING_DELIVERY_DATE", shipment
    received_dt = _dt(received)
    delivered_dt = _dt(delivered)
    age_days = (received_dt.date() - delivered_dt.date()).days
    if age_days < 0:
        return "UNABLE", "DATE_INCONSISTENT", shipment
    return "OK", age_days, shipment


def _movement_context(facts: Any, data: PreparedData) -> tuple[str, Any, dict[str, Any] | None]:
    refs = _order_refs(facts)
    if len(refs) != 1:
        return "UNABLE", RC_ORDER_AMBIGUOUS, None
    shipment = data.shipment(refs[0])
    if shipment is None:
        return "UNABLE", "NO_SHIPMENT", None
    if int(shipment.get("carrier_status_usable", 0)) == 0:
        return "UNABLE", RC_CARRIER_UNAVAILABLE, shipment
    if int(shipment.get("movement_check_usable", 0)) == 0 or shipment.get("flag_codes"):
        return "UNABLE", "SHIPMENT_FLAGGED", shipment
    received = _received_at(facts)
    last_event = shipment.get("last_event_at_utc")
    if not received or not last_event:
        return "UNABLE", "MISSING_CARRIER_EVENT", shipment
    elapsed_seconds = (_dt(received) - _dt(last_event)).total_seconds()
    if elapsed_seconds < 0:
        return "UNABLE", "DATE_INCONSISTENT", shipment
    return "OK", round(elapsed_seconds / 86400.0, 3), shipment


def _non_delivery(facts: Any) -> bool | None:
    flag = _bool_fact(facts, "non_delivery_claim")
    if flag is not None:
        return flag
    if _ticket_type(facts) == "DELIVERY_ISSUE":
        return True
    return None


def check_2_1_delivery_window(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "2.1_delivery_window", "2.1"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    status, value, shipment = _delivery_context(facts, data)
    if status == "ERROR":
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("shipments", "delivery", "order unresolved")],
                      {"failure_reason": value})
    if status == "UNABLE":
        evidence = [_evidence("shipments", (shipment or {}).get("shipment_id", "-"), value)]
        return _check(check_id, UNABLE_TO_VERIFY, clause, evidence, {"failure_reason": value})
    age_days = int(value)
    result = PASS if age_days <= ORDINARY_WINDOW_DAYS else FAIL
    details = {"age_days": age_days, "window_days": ORDINARY_WINDOW_DAYS}
    evidence = [
        _evidence("shipments", shipment["shipment_id"], f"delivered_at={shipment['delivered_at_utc']}"),
        _evidence("ticket", "received_at", _received_at(facts)),
        _evidence("policy", "2.1", f"{age_days} days since delivery (limit {ORDINARY_WINDOW_DAYS})"),
    ]
    return _check(check_id, result, clause, evidence, details)


def check_2_2_affected_lines(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "2.2_affected_lines", "2.2"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    calculation = _calculate(facts, data)
    if "error" in calculation:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("order_items", _order_refs(facts)[0] if _order_refs(facts) else "-", calculation["error"])],
            {"failure_reason": calculation["error"]},
        )
    evidence = [
        _evidence(
            "order_items",
            f"{item['order_id']}:{item['line_no']}",
            f"{item['product_name']} qty={item['affected_qty']} x {item['unit_price_usd']:.2f} = {item['line_value_usd']:.2f}",
        )
        for item in calculation["affected"]
    ]
    details = {
        "affected_lines": calculation["affected"],
        "line_value_total": calculation["line_value_total"],
        "quantity_clamped": any(item["quantity_clamped"] for item in calculation["affected"]),
    }
    return _check(check_id, PASS, clause, evidence, details)


def check_2_3_out_of_window(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "2.3_out_of_window", "2.3"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    status, value, shipment = _delivery_context(facts, data)
    if status != "OK":
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("shipments", "delivery", value)],
                      {"failure_reason": value})
    age_days = int(value)
    if age_days <= ORDINARY_WINDOW_DAYS:
        return _check(
            check_id,
            PASS,
            clause,
            [_evidence("policy", "2.3", f"{age_days} days <= {ORDINARY_WINDOW_DAYS}; in window")],
            {"age_days": age_days},
        )
    damaged = _bool_fact(facts, "damaged_or_faulty")
    photo = _bool_fact(facts, "has_photo_attachment")
    if damaged is None or (damaged and photo is None):
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("ticket", "damage/photo", "missing evidence")],
            {"failure_reason": "MISSING_DAMAGE_EVIDENCE", "age_days": age_days},
        )
    exception_applies = bool(damaged and photo and 31 <= age_days <= DAMAGE_WINDOW_DAYS)
    result = PASS if exception_applies else FAIL
    details = {"age_days": age_days, "damage_exception": exception_applies}
    evidence = [
        _evidence("policy", "2.3", f"{age_days} days since delivery"),
        _evidence("policy", "2.6", f"damaged={bool(damaged)}, photo={bool(photo)}, exception={exception_applies}"),
    ]
    return _check(check_id, result, clause, evidence, details)


def check_2_4_partial_lines(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "2.4_partial_lines", "2.4"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    whole_order = _bool_fact(facts, "whole_order_requested")
    shipping = _bool_fact(facts, "shipping_refund_requested")
    if whole_order is None or shipping is None:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("ticket", "requested_scope", "missing scope facts")],
            {"failure_reason": RC_SCOPE_UNVERIFIABLE},
        )
    reduced = bool(whole_order or shipping)
    result = FAIL if reduced else PASS
    details = {
        "whole_order_requested": bool(whole_order),
        "shipping_refund_requested": bool(shipping),
        "amount_limited_to_affected_lines": True,
    }
    evidence = [
        _evidence("ticket", "scope", f"whole_order={bool(whole_order)}, shipping={bool(shipping)}"),
        _evidence("policy", "2.4", "refund affected lines only" if reduced else "request already limited to affected lines"),
    ]
    return _check(check_id, result, clause, evidence, details)


def check_2_5_prior_refunds(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "2.5_prior_refunds", "2.5"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    calculation = _calculate(facts, data)
    if "error" in calculation:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("refunds", "affected_lines", calculation["error"])],
            {"failure_reason": calculation["error"]},
        )
    affected = calculation["affected"]
    refunded_on_affected = round(sum(item["refunded_usd"] for item in affected), 2)
    fully_paid = bool(affected) and all(item["unpaid_usd"] <= 0 for item in affected)
    if fully_paid and refunded_on_affected > 0:
        result = FAIL
    else:
        result = PASS
    details = {
        "refundable_amount": calculation["refundable_amount"],
        "line_value_total": calculation["line_value_total"],
        "has_prior_refund": calculation["has_prior_refund"],
        "prior_refunds": calculation["prior_refunds"],
        "affected_refunded_usd": refunded_on_affected,
        "all_affected_paid": fully_paid,
    }
    evidence = [
        _evidence(
            "refunds",
            item["order_id"],
            f"line {item['line_no']}: refunded={item['refunded_usd']:.2f}, unpaid={item['unpaid_usd']:.2f}",
        )
        for item in affected
    ]
    if calculation["prior_refunds"]:
        evidence.append(
            _evidence(
                "refunds",
                calculation["order_id"],
                "; ".join(
                    f"{refund['refund_id']} line {refund['line_no']} ${refund['amount_usd']:.2f}"
                    for refund in calculation["prior_refunds"]
                ),
            )
        )
    return _check(check_id, result, clause, evidence, details)


def check_2_6_damage_exception(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "2.6_damage_exception", "2.6"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    damaged = _bool_fact(facts, "damaged_or_faulty")
    if damaged is None:
        return _check(check_id, UNABLE_TO_VERIFY, clause,
                      [_evidence("ticket", "damage", "missing evidence")],
                      {"failure_reason": "MISSING_DAMAGE_EVIDENCE"})
    if not damaged:
        return _check(
            check_id,
            NOT_APPLICABLE,
            clause,
            [_evidence("ticket", "damage", "not damaged/faulty")],
            {"reason": "NOT_DAMAGED"},
        )
    status, value, shipment = _delivery_context(facts, data)
    if status != "OK":
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("shipments", "delivery", value)],
                      {"failure_reason": value})
    age_days = int(value)
    if age_days <= ORDINARY_WINDOW_DAYS:
        return _check(
            check_id,
            NOT_APPLICABLE,
            clause,
            [_evidence("policy", "2.6", f"{age_days} days; ordinary window already applies")],
            {"age_days": age_days, "extension_needed": False},
        )
    photo = _bool_fact(facts, "has_photo_attachment")
    if photo is None:
        return _check(check_id, UNABLE_TO_VERIFY, clause,
                      [_evidence("ticket", "attachment", "missing photo fact")],
                      {"failure_reason": "MISSING_PHOTO_EVIDENCE", "age_days": age_days})
    applies = bool(photo and age_days <= DAMAGE_WINDOW_DAYS)
    details = {"age_days": age_days, "photo": bool(photo), "extension_days": DAMAGE_WINDOW_DAYS, "applies": applies}
    evidence = [
        _evidence("ticket", "attachment", f"image_attachment={bool(photo)}"),
        _evidence("shipments", (shipment or {}).get("shipment_id", "-"), f"age_days={age_days}"),
        _evidence("policy", "2.6", f"damage exception {'applies' if applies else 'does not apply'}"),
    ]
    return _check(check_id, PASS if applies else FAIL, clause, evidence, details)


def check_3_1_auto_authority(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "3.1_auto_authority", "3.1"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    calculation = _calculate(facts, data)
    if "error" in calculation:
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("amount", "refundable", calculation["error"])],
                      {"failure_reason": calculation["error"]})
    amount = calculation["refundable_amount"]
    result = PASS if amount <= AUTO_APPROVAL_LIMIT else FAIL
    details = {"refundable_amount": amount, "auto_approval_limit": AUTO_APPROVAL_LIMIT}
    evidence = [
        _evidence("amount", "refundable", f"${amount:.2f}"),
        _evidence("policy", "3.1", f"limit ${AUTO_APPROVAL_LIMIT:.2f}"),
    ]
    return _check(check_id, result, clause, evidence, details)


def check_3_2_lead_approval(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "3.2_lead_approval", "3.2"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    calculation = _calculate(facts, data)
    if "error" in calculation:
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("amount", "refundable", calculation["error"])],
                      {"failure_reason": calculation["error"]})
    amount = calculation["refundable_amount"]
    approval_required = amount > AUTO_APPROVAL_LIMIT
    details = {
        "refundable_amount": amount,
        "approval_required": approval_required,
        "approver_role": "SUPPORT_LEAD" if approval_required else None,
    }
    evidence = [
        _evidence("amount", "refundable", f"${amount:.2f}"),
        _evidence("policy", "3.2", "approval required" if approval_required else "no approval required"),
    ]
    return _check(check_id, FAIL if approval_required else PASS, clause, evidence, details)


def check_3_3_process_once(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "3.3_process_once", "3.3"
    if _ticket_type(facts) not in REFUND_TYPES:
        return _not_applicable(check_id, clause, "not a refund/return")
    already_processed = _bool_fact(facts, "already_processed")
    if already_processed is None:
        return _check(
            check_id,
            NOT_APPLICABLE,
            clause,
            [_evidence("ledger", "request", "no request-ledger evidence supplied")],
            {"reason": "NO_LEDGER_CONTEXT"},
        )
    details = {"already_processed": already_processed}
    return _check(
        check_id,
        FAIL if already_processed else PASS,
        clause,
        [_evidence("ledger", "request", f"already_processed={already_processed}")],
        details,
    )


def check_4_1_identity(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "4.1_identity", "4.1"
    refs = _order_refs(facts)
    if len(refs) > 1:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("ticket", "order_ref", ", ".join(refs))],
            {"failure_reason": RC_ORDER_AMBIGUOUS},
        )
    if not refs:
        return _check(
            check_id,
            FAIL,
            clause,
            [_evidence("ticket", "order_ref", "no order reference found")],
            {"failure_reason": RC_ORDER_NOT_FOUND},
        )
    order_id = refs[0]
    order = data.order(order_id)
    if order is None:
        return _check(
            check_id,
            FAIL,
            clause,
            [_evidence("orders", order_id, "order not found")],
            {"failure_reason": RC_ORDER_NOT_FOUND, "order_id": order_id},
        )
    if order.get("data_quality_status") == "AMBIGUOUS":
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("orders", order_id, "order marked AMBIGUOUS")],
            {"failure_reason": RC_ORDER_AMBIGUOUS, "order_id": order_id},
        )
    email = _sender_email(facts)
    if not email:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("ticket", "from", "missing sender email")],
            {"failure_reason": RC_IDENTITY_UNVERIFIABLE, "order_id": order_id},
        )
    customer = data.customer_by_email(email)
    if customer is None:
        return _check(
            check_id,
            FAIL,
            clause,
            [_evidence("customers", email, "no customer on file")],
            {"failure_reason": RC_CUSTOMER_MISMATCH, "order_id": order_id},
        )
    if customer["customer_id"] != order["customer_id"]:
        return _check(
            check_id,
            FAIL,
            clause,
            [
                _evidence("orders", order_id, f"belongs to {order['customer_id']}"),
                _evidence("customers", customer["customer_id"], f"email {email}"),
            ],
            {
                "failure_reason": RC_CUSTOMER_MISMATCH,
                "order_id": order_id,
                "order_customer_id": order["customer_id"],
                "sender_customer_id": customer["customer_id"],
            },
        )
    return _check(
        check_id,
        PASS,
        clause,
        [
            _evidence("orders", order_id, "order exists"),
            _evidence("customers", customer["customer_id"], f"email {email} matches order owner"),
        ],
        {"order_id": order_id, "customer_id": customer["customer_id"]},
    )


def check_4_2_carrier_status(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "4.2_carrier_status", "4.2"
    if not (_non_delivery(facts) is True or _ticket_type(facts) == "DELIVERY_ISSUE"):
        return _not_applicable(check_id, clause, "claim does not depend on carrier status")
    refs = _order_refs(facts)
    if len(refs) != 1:
        return _not_applicable(check_id, clause, "order unresolved")
    shipment = data.shipment(refs[0])
    if shipment is None:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("shipments", refs[0], "no shipment record")],
            {"failure_reason": "NO_SHIPMENT"},
        )
    status = shipment.get("status")
    if int(shipment.get("carrier_status_usable", 0)) == 0 or status not in {"DELIVERED", "IN_TRANSIT"}:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("shipments", shipment["shipment_id"], f"status={status}")],
            {"failure_reason": RC_CARRIER_UNAVAILABLE, "status": status},
        )
    return _check(
        check_id,
        PASS,
        clause,
        [_evidence("shipments", shipment["shipment_id"], f"status={status}")],
        {"status": status, "last_event_at_utc": shipment.get("last_event_at_utc")},
    )


def check_5_1_lost_in_transit(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "5.1_lost_in_transit", "5.1"
    if not (_non_delivery(facts) is True or _ticket_type(facts) == "DELIVERY_ISSUE"):
        return _not_applicable(check_id, clause, "not a non-delivery claim")
    refs = _order_refs(facts)
    if len(refs) != 1:
        return _not_applicable(check_id, clause, "order unresolved")
    shipment = data.shipment(refs[0])
    if shipment is None or int(shipment.get("carrier_status_usable", 0)) == 0:
        return _not_applicable(check_id, clause, "carrier status handled by 4.2")
    if shipment.get("status") != "IN_TRANSIT":
        return _not_applicable(check_id, clause, "shipment is not in transit")
    status, value, shipment = _movement_context(facts, data)
    if status != "OK":
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("shipments", (shipment or {}).get("shipment_id", refs[0]), value)],
            {"failure_reason": RC_MOVEMENT_UNVERIFIABLE, "data_issue": value},
        )
    elapsed_days = float(value)
    lost = elapsed_days > MOVEMENT_LOST_DAYS
    details = {"elapsed_days": elapsed_days, "limit_days": MOVEMENT_LOST_DAYS, "lost": lost}
    evidence = [
        _evidence("shipments", shipment["shipment_id"], f"last_event_at={shipment.get('last_event_at_utc')}"),
        _evidence("ticket", "received_at", _received_at(facts)),
        _evidence("policy", "5.1", f"{elapsed_days:.3f} days without movement"),
    ]
    return _check(check_id, FAIL if lost else PASS, clause, evidence, details)


def check_5_2_override_attempt(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "5.2_override_attempt", "5.2"
    override = _bool_fact(facts, "override_attempt")
    if override is None:
        return _check(
            check_id,
            UNABLE_TO_VERIFY,
            clause,
            [_evidence("ticket", "body", "missing override-attempt fact")],
            {"failure_reason": "MISSING_OVERRIDE_CHECK"},
        )
    return _check(
        check_id,
        FAIL if override else PASS,
        clause,
        [_evidence("ticket", "body", f"override_attempt={override}")],
        {"override_attempt": override},
    )


def check_5_3_order_status(facts: Any, data: PreparedData) -> dict[str, Any]:
    check_id, clause = "5.3_order_status", "5.3"
    if _ticket_type(facts) != "ORDER_STATUS":
        return _not_applicable(check_id, clause, "not an order-status question")
    refs = _order_refs(facts)
    if len(refs) != 1:
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("ticket", "order_ref", "unresolved")],
                      {"failure_reason": RC_ORDER_AMBIGUOUS if len(refs) > 1 else RC_ORDER_NOT_FOUND})
    shipment = data.shipment(refs[0])
    if shipment is None:
        return _check(check_id, UNABLE_TO_VERIFY, clause, [_evidence("shipments", refs[0], "no shipment")],
                      {"failure_reason": "NO_SHIPMENT"})
    if int(shipment.get("carrier_status_usable", 0)) == 0:
        return _check(check_id, UNABLE_TO_VERIFY, clause,
                      [_evidence("shipments", shipment["shipment_id"], f"status={shipment.get('status')}")],
                      {"failure_reason": RC_CARRIER_UNAVAILABLE})
    if int(shipment.get("movement_check_usable", 0)) == 0 or shipment.get("flag_codes"):
        return _check(check_id, UNABLE_TO_VERIFY, clause,
                      [_evidence("shipments", shipment["shipment_id"], ",".join(shipment.get("flag_codes", [])))],
                      {"failure_reason": RC_MOVEMENT_UNVERIFIABLE})
    return _check(
        check_id,
        PASS,
        clause,
        [
            _evidence("shipments", shipment["shipment_id"], f"status={shipment.get('status')}"),
            _evidence("shipments", shipment["shipment_id"], f"last_event_at={shipment.get('last_event_at_utc')}"),
        ],
        {"status": shipment.get("status"), "last_event_at_utc": shipment.get("last_event_at_utc")},
    )


CHECK_FUNCTIONS: tuple[Callable[[Any, PreparedData], dict[str, Any]], ...] = (
    check_2_1_delivery_window,
    check_2_2_affected_lines,
    check_2_3_out_of_window,
    check_2_4_partial_lines,
    check_2_5_prior_refunds,
    check_2_6_damage_exception,
    check_3_1_auto_authority,
    check_3_2_lead_approval,
    check_3_3_process_once,
    check_4_1_identity,
    check_4_2_carrier_status,
    check_5_1_lost_in_transit,
    check_5_2_override_attempt,
    check_5_3_order_status,
)


def run_checks(ticket_facts: Any, data: PreparedData) -> list[dict[str, Any]]:
    """Run every clause check once, in clause order."""
    return [check(ticket_facts, data) for check in CHECK_FUNCTIONS]


def evaluate(ticket_facts: Any, data: PreparedData) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checks = run_checks(ticket_facts, data)
    return checks, decide(ticket_facts, checks)


def _decision(
    outcome: str,
    reason_codes: Iterable[str],
    human_approval_required: bool,
    approver_role: str | None,
    refundable_amount: float | None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "human_approval_required": bool(human_approval_required),
        "approver_role": approver_role,
        "refundable_amount": None if refundable_amount is None else round(float(refundable_amount), 2),
    }


def decide(ticket_facts: Any, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the ordered rule table to the clause checks."""
    by_id = {check["check_id"]: check for check in checks}

    def result(check_id: str) -> str:
        check = by_id.get(check_id)
        return UNABLE_TO_VERIFY if check is None else check["result"]

    def details(check_id: str) -> dict[str, Any]:
        return (by_id.get(check_id) or {}).get("details", {})

    # 1. Trust & Safety hard stop.
    override_result = result("5.2_override_attempt")
    if _bool_fact(ticket_facts, "override_attempt") is True or override_result == FAIL:
        return _decision("ESCALATE", [RC_OVERRIDE], True, "TRUST_SAFETY", None)
    if override_result == UNABLE_TO_VERIFY:
        return _decision("UNABLE_TO_VERIFY", [RC_SAFETY_UNVERIFIABLE], False, None, None)

    # 2. Identity gate.
    identity_result = result("4.1_identity")
    if identity_result == FAIL:
        reason = details("4.1_identity").get("failure_reason") or RC_ORDER_NOT_FOUND
        return _decision("INVALID_REQUEST", [reason], False, None, None)
    if identity_result != PASS:
        reason = details("4.1_identity").get("failure_reason") or RC_IDENTITY_UNVERIFIABLE
        return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)

    ticket_type = _ticket_type(ticket_facts)
    if ticket_type is None:
        return _decision("UNABLE_TO_VERIFY", [RC_UNCLASSIFIED], False, None, None)

    # 3. Order-status questions.
    if ticket_type == "ORDER_STATUS":
        status_result = result("5.3_order_status")
        if status_result == PASS:
            return _decision("PROCEED", [], False, None, None)
        if status_result == UNABLE_TO_VERIFY:
            reason = details("5.3_order_status").get("failure_reason") or RC_MOVEMENT_UNVERIFIABLE
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)
        return _decision("PROCEED", [], False, None, None)

    # 4. Delivery / non-delivery claims.
    if ticket_type == "DELIVERY_ISSUE" or _non_delivery(ticket_facts) is True:
        carrier_result = result("4.2_carrier_status")
        if carrier_result == UNABLE_TO_VERIFY:
            return _decision("UNABLE_TO_VERIFY", [RC_CARRIER_UNAVAILABLE], False, None, None)
        lost_result = result("5.1_lost_in_transit")
        if lost_result == FAIL:
            return _decision("ESCALATE", [RC_LOST], True, "CARRIER_CLAIMS", None)
        if lost_result == UNABLE_TO_VERIFY:
            reason = details("5.1_lost_in_transit").get("failure_reason") or RC_MOVEMENT_UNVERIFIABLE
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)
        return _decision("PROCEED", [], False, None, None)

    # 5. Refund / return.
    if ticket_type in REFUND_TYPES:
        process_once = result("3.3_process_once")
        if process_once == FAIL:
            return _decision("REJECT", [RC_ALREADY_PROCESSED], False, None, None)

        window_result = result("2.3_out_of_window")
        if window_result == FAIL:
            return _decision("REJECT", [RC_OUT_OF_WINDOW], False, None, None)
        if window_result != PASS:
            reason = details("2.3_out_of_window").get("failure_reason") or RC_DELIVERY_DATE_UNAVAILABLE
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)

        lines_result = result("2.2_affected_lines")
        if lines_result != PASS:
            reason = details("2.2_affected_lines").get("failure_reason") or RC_AFFECTED_LINES_UNVERIFIABLE
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)

        prior_result = result("2.5_prior_refunds")
        if prior_result == FAIL:
            return _decision("REJECT", [RC_ALREADY_REFUNDED], False, None, None)
        if prior_result != PASS:
            reason = details("2.5_prior_refunds").get("failure_reason") or "PRIOR_REFUND_UNVERIFIABLE"
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)

        scope_result = result("2.4_partial_lines")
        if scope_result == UNABLE_TO_VERIFY:
            reason = details("2.4_partial_lines").get("failure_reason") or RC_SCOPE_UNVERIFIABLE
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)

        authority_result = result("3.2_lead_approval")
        if authority_result == UNABLE_TO_VERIFY:
            reason = details("3.2_lead_approval").get("failure_reason") or "AMOUNT_UNVERIFIABLE"
            return _decision("UNABLE_TO_VERIFY", [reason], False, None, None)

        refundable_amount = details("2.5_prior_refunds").get("refundable_amount")
        if refundable_amount is None:
            refundable_amount = details("2.2_affected_lines").get("line_value_total")

        reasons: list[str] = []
        if result("2.6_damage_exception") == PASS:
            reasons.append(RC_DAMAGE_EXCEPTION)
        if details("2.5_prior_refunds").get("has_prior_refund"):
            reasons.append(RC_ALREADY_REFUNDED)
        if scope_result == FAIL:
            reasons.append(RC_PARTIAL_LINES)
        if authority_result == FAIL:
            reasons.append(RC_APPROVAL)

        if authority_result == FAIL:
            return _decision("PEND", reasons, True, "SUPPORT_LEAD", refundable_amount)
        if scope_result == FAIL:
            return _decision("PROCEED_MODIFIED", reasons, False, None, refundable_amount)
        return _decision("PROCEED", reasons, False, None, refundable_amount)

    # 6. Anything else is answered without a refund action.
    return _decision("PROCEED", [], False, None, None)
