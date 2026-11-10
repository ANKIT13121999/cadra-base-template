from __future__ import annotations

import json
import unittest

from src.extract import extract_ticket, parse_order_candidates
from src.llm import LLMError, ModelCallResult
from src.prepare import resolve_data_room

DATA_ROOM = resolve_data_room(None)
PUBLIC = json.loads((DATA_ROOM / "inbox" / "public_tickets.json").read_text(encoding="utf-8"))
PUBLIC_BY_ID = {ticket["ticket_id"]: ticket for ticket in PUBLIC}


class FailingLLM:
    configured = True

    def chat(self, *args, **kwargs):
        raise LLMError("simulated failure")


class BadJSONLLM:
    configured = True

    def chat(self, *args, **kwargs):
        return ModelCallResult(
            content="not json",
            model="fake",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0001,
            latency_ms=1,
        )


class ExtractionTests(unittest.TestCase):
    def test_order_id_variants_normalise_and_dedupe(self) -> None:
        text = "LF-100216, lf 100216, #100216, LF100216, order order 100216"
        self.assertEqual(parse_order_candidates(text), ["LF-100216"])

    def test_deterministic_injection_flag(self) -> None:
        extraction = extract_ticket(PUBLIC_BY_ID["P05"], use_llm=False)
        self.assertTrue(extraction.injection_suspected)
        self.assertEqual(extraction.order_id_candidates, ["LF-100228"])

    def test_deterministic_types(self) -> None:
        status = extract_ticket(PUBLIC_BY_ID["P03"], use_llm=False)
        self.assertEqual(status.ticket_type, "ORDER_STATUS")
        refund = extract_ticket(PUBLIC_BY_ID["P02"], use_llm=False)
        self.assertEqual(refund.ticket_type, "REFUND")
        delivery = extract_ticket(PUBLIC_BY_ID["P09"], use_llm=False)
        self.assertEqual(delivery.ticket_type, "DELIVERY_ISSUE")
        self.assertTrue(delivery.non_delivery_claim)

    def test_forwarded_original_sender(self) -> None:
        tickets = json.loads((__import__("pathlib").Path(__file__).resolve().parents[2] / "tickets" / "support_inbox_export.json").read_text(encoding="utf-8"))
        forwarded = next(ticket for ticket in tickets if ticket["ticket_id"] == "T027")
        extraction = extract_ticket(forwarded, use_llm=False)
        self.assertTrue(extraction.is_forward)
        self.assertEqual(extraction.original_sender, "ben.tanaka1@mail.example")
        self.assertEqual(extraction.original_subject, "Damaged Arc floor lamp")

    def test_llm_failure_uses_backup(self) -> None:
        extraction = extract_ticket(PUBLIC_BY_ID["P05"], llm=FailingLLM())
        self.assertEqual(extraction.source, "fallback")
        self.assertIn("LLMError", extraction.error or "")
        self.assertTrue(extraction.injection_suspected)
        self.assertIn("LF-100228", extraction.order_id_candidates)

    def test_bad_json_uses_backup_but_keeps_real_call(self) -> None:
        extraction = extract_ticket(PUBLIC_BY_ID["P05"], llm=BadJSONLLM())
        self.assertEqual(extraction.source, "fallback")
        self.assertIsNotNone(extraction.model_call)
        self.assertTrue(extraction.injection_suspected)


if __name__ == "__main__":
    unittest.main()
