"""Pipeline: extract -> resolve -> checks -> decide -> draft -> trace."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from schema.response_schema import TicketResponse
from src.checks import evaluate
from src.draft import build_draft, render_template
from src.extract import Extraction, extract_ticket
from src.facts import AffectedItem, TicketFacts
from src.llm import LLMClient
from src.normalize import normalize_email, normalize_text, ticket_dedupe_key
from src.repo import PreparedData

DOCUMENT = {"document": "refund_policy_RP-3.2.md", "version": "3.2"}


@dataclass
class PipelineResult:
    response: dict[str, Any]
    extraction: Extraction
    decision: dict[str, Any]
    checks: list[dict[str, Any]]
    dedupe_key: str
    latency_ms: int
    error: str | None = None


def _has_image_attachment(ticket: Mapping[str, Any]) -> bool:
    for attachment in ticket.get("attachments") or []:
        if not isinstance(attachment, Mapping):
            continue
        content_type = str(attachment.get("content_type") or "").lower()
        file_name = str(attachment.get("file_name") or "").lower()
        if content_type.startswith("image/"):
            return True
        if any(file_name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")):
            return True
    return False


def _matches_item(item: Mapping[str, Any], line: Mapping[str, Any]) -> bool:
    sku = str(item.get("sku") or "").strip().lower()
    if sku and str(line.get("sku") or "").strip().lower() == sku:
        return True
    name = normalize_text(item.get("product_name"))
    line_name = normalize_text(line.get("product_name"))
    if not name or not line_name:
        return False
    return name == line_name or name in line_name or line_name in name


def _verified_items(
    extraction: Extraction,
    data: PreparedData,
    order_id: str | None,
    ticket: Mapping[str, Any],
) -> list[AffectedItem]:
    if not order_id:
        return []
    lines = data.lines(order_id)
    verified: list[AffectedItem] = []
    for item in extraction.claimed_items:
        for line in lines:
            if _matches_item(item, line):
                verified.append(
                    AffectedItem(
                        product_name=line["product_name"],
                        sku=line["sku"],
                        quantity=max(1, int(item.get("quantity") or 1)),
                    )
                )
                break
    if verified:
        return verified

    text = normalize_text(f"{ticket.get('subject') or ''} {ticket.get('body') or ''}")
    derived: list[AffectedItem] = []
    for line in lines:
        product = normalize_text(line.get("product_name"))
        if product and product in text:
            derived.append(AffectedItem(product_name=line["product_name"], sku=line["sku"], quantity=1))
    return derived


def _resolve(
    extraction: Extraction,
    data: PreparedData,
    sender_email: str,
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    customer = data.customer_by_email(sender_email)
    candidates = list(dict.fromkeys(extraction.order_id_candidates))
    owned: list[str] = []
    if customer is not None:
        for ref in candidates:
            order = data.order(ref)
            if order is not None and order["customer_id"] == customer["customer_id"]:
                owned.append(ref)
    if len(owned) == 1:
        return data.order(owned[0]), candidates, owned[0]
    if len(owned) > 1:
        return None, candidates, owned  # type: ignore[return-value]
    if candidates:
        return None, candidates, candidates[0]
    return None, candidates, None


def _trace(
    extraction: Extraction,
    draft_model_call: Any,
    latency_ms: int,
    llm_error: str | None = None,
) -> dict[str, Any]:
    model_calls = []
    cost = 0.0
    if extraction.model_call is not None:
        model_calls.append(extraction.model_call.as_model_call())
        cost += extraction.model_call.cost_usd
    if draft_model_call is not None:
        model_calls.append(draft_model_call.as_model_call())
        cost += draft_model_call.cost_usd
    return {
        "model_calls": model_calls,
        "tools_called": ["extract", "resolve", "checks", "decide", "draft"],
        "documents_used": [DOCUMENT],
        "measured_cost_usd": round(cost, 8),
        "llm_error": llm_error,
    }


def _validate_response(payload: dict[str, Any]) -> dict[str, Any]:
    validated = TicketResponse.model_validate(payload)
    return validated.model_dump()


def _fallback_response(
    ticket: Mapping[str, Any],
    run_id: str,
    extraction: Extraction,
    latency_ms: int,
    error: str,
) -> dict[str, Any]:
    sender = extraction.original_sender or normalize_email(ticket.get("from"))
    subject = f"Re: {extraction.original_subject}" if extraction.original_subject else "Your Larkfield Home request"
    draft = render_template(
        "UNABLE_TO_VERIFY",
        ["INTERNAL_ERROR"],
        None,
        None,
        None,
        subject,
        sender,
    )
    payload = {
        "ticket_id": str(ticket.get("ticket_id") or ""),
        "run_id": run_id,
        "ticket_type": extraction.ticket_type if extraction.ticket_type in {"REFUND", "RETURN", "ORDER_STATUS", "DELIVERY_ISSUE", "OTHER"} else "OTHER",
        "resolved": {"order_id": None, "customer_id": None},
        "outcome": "UNABLE_TO_VERIFY",
        "reason_codes": ["INTERNAL_ERROR"],
        "checks": [],
        "human_approval_required": False,
        "approver_role": None,
        "draft_reply": draft,
        "trace": _trace(extraction, None, latency_ms, extraction.error or error),
    }
    response = _validate_response(payload)
    response["latency_ms"] = latency_ms
    return response


def process_ticket(
    ticket: Mapping[str, Any],
    data: PreparedData,
    *,
    run_id: str | None = None,
    llm: LLMClient | None = None,
    use_llm: bool = True,
) -> PipelineResult:
    started = time.perf_counter()
    run_id = run_id or str(uuid.uuid4())
    extraction: Extraction | None = None
    try:
        extraction = extract_ticket(ticket, llm=llm, use_llm=use_llm)
    except Exception as exc:  # noqa: BLE001 - the pipeline must never error
        try:
            extraction = extract_ticket(ticket, use_llm=False)
        except Exception:
            extraction = Extraction(
                ticket_id=str(ticket.get("ticket_id") or ""),
                ticket_type="OTHER",
                order_id_candidates=[],
                claimed_items=[],
                damage_claimed=False,
                photo_mentioned=False,
                requested_amount=None,
                original_sender=normalize_email(ticket.get("from")),
                original_subject=str(ticket.get("subject") or ""),
                injection_suspected=False,
                is_forward=False,
                wrong_or_not_as_described=False,
                non_delivery_claim=False,
                whole_order_requested=False,
                shipping_refund_requested=False,
                source="fallback",
                error=str(exc),
            )

    try:
        sender_email = normalize_email(extraction.original_sender or ticket.get("from"))
        resolved_order, candidates, order_ref = _resolve(extraction, data, sender_email)
        claimed_items = _verified_items(extraction, data, resolved_order["order_id"] if resolved_order else None, ticket)
        facts = TicketFacts(
            ticket_id=str(ticket.get("ticket_id") or ""),
            received_at_utc=str(ticket.get("received_at") or ""),
            customer_email=sender_email,
            ticket_type=extraction.ticket_type,
            order_ref=order_ref,
            affected_items=tuple(claimed_items),
            damaged_or_faulty=extraction.damage_claimed,
            wrong_or_not_as_described=extraction.wrong_or_not_as_described,
            non_delivery_claim=extraction.non_delivery_claim,
            whole_order_requested=extraction.whole_order_requested,
            shipping_refund_requested=extraction.shipping_refund_requested,
            override_attempt=extraction.injection_suspected,
            has_photo_attachment=_has_image_attachment(ticket),
            already_processed=False,
        )
        checks, decision = evaluate(facts, data)
        identity = next((check for check in checks if check["check_id"] == "4.1_identity"), None)
        resolved_payload = {"order_id": None, "customer_id": None}
        if identity is not None and identity["result"] == "PASS":
            details = identity.get("details") or {}
            resolved_payload = {
                "order_id": details.get("order_id"),
                "customer_id": details.get("customer_id"),
            }
        draft_result = build_draft(
            ticket,
            extraction,
            decision,
            resolved_payload.get("order_id"),
            llm=llm,
            use_llm=use_llm,
        )
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        llm_error = extraction.error
        if draft_result.fallback_reason and draft_result.fallback_reason != "deterministic outcome":
            draft_error = draft_result.fallback_reason
            if draft_result.violations:
                draft_error += ": " + ", ".join(draft_result.violations)
            llm_error = f"{llm_error}; {draft_error}" if llm_error else draft_error
        if llm_error:
            llm_error = llm_error[:300]
        payload = {
            "ticket_id": str(ticket.get("ticket_id") or ""),
            "run_id": run_id,
            "ticket_type": extraction.ticket_type,
            "resolved": resolved_payload,
            "outcome": decision["outcome"],
            "reason_codes": decision["reason_codes"],
            "checks": checks,
            "human_approval_required": decision["human_approval_required"],
            "approver_role": decision["approver_role"],
            "draft_reply": draft_result.draft,
            "trace": _trace(extraction, draft_result.model_call, latency_ms, llm_error),
        }
        response = _validate_response(payload)
        response["latency_ms"] = latency_ms
        dedupe_key = ticket_dedupe_key(
            extraction.original_sender or sender_email,
            extraction.original_subject,
            str(ticket.get("body") or ""),
        )
        return PipelineResult(
            response=response,
            extraction=extraction,
            decision=decision,
            checks=checks,
            dedupe_key=dedupe_key,
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 - never return an error response
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        response = _fallback_response(ticket, run_id, extraction, latency_ms, str(exc))
        return PipelineResult(
            response=response,
            extraction=extraction,
            decision={
                "outcome": "UNABLE_TO_VERIFY",
                "reason_codes": ["INTERNAL_ERROR"],
                "human_approval_required": False,
                "approver_role": None,
                "refundable_amount": None,
            },
            checks=[],
            dedupe_key=ticket_dedupe_key(
                extraction.original_sender or normalize_email(ticket.get("from")),
                extraction.original_subject,
                str(ticket.get("body") or ""),
            ),
            latency_ms=latency_ms,
            error=str(exc),
        )
