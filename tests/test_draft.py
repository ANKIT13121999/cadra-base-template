from __future__ import annotations

import json
import unittest

from src.draft import build_draft, render_template, validate_draft
from src.llm import ModelCallResult


class BadDraftLLM:
    configured = True

    def chat(self, *args, **kwargs):
        return ModelCallResult(
            content=json.dumps(
                {
                    "subject": "Re: test",
                    "body": "We will process a refund of $99.00 for order LF-100216.",
                }
            ),
            model="fake",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.0001,
            latency_ms=1,
        )


class ExplodingLLM:
    configured = True

    def chat(self, *args, **kwargs):
        raise AssertionError("LLM must not be called for template outcomes")


class DraftValidationTests(unittest.TestCase):
    def test_amount_and_order_match(self) -> None:
        violations = validate_draft(
            {"body": "The proposed refund is $100.00 for order LF-100216."},
            outcome="PEND",
            refundable_amount=100.0,
            order_id="LF-100216",
        )
        self.assertEqual(violations, [])

    def test_wrong_amount_fails(self) -> None:
        violations = validate_draft(
            {"body": "The proposed refund is $99.00 for order LF-100216."},
            outcome="PEND",
            refundable_amount=100.0,
            order_id="LF-100216",
        )
        self.assertIn("dollar amount does not equal the refundable amount", violations)

    def test_wrong_order_fails(self) -> None:
        violations = validate_draft(
            {"body": "The proposed refund is $100.00 for order LF-100217."},
            outcome="PEND",
            refundable_amount=100.0,
            order_id="LF-100216",
        )
        self.assertIn("body mentions a different order id", violations)

    def test_refund_promise_for_pending_fails(self) -> None:
        violations = validate_draft(
            {"body": "We will process a refund of $100.00 for order LF-100216."},
            outcome="PEND",
            refundable_amount=100.0,
            order_id="LF-100216",
        )
        self.assertIn("body promises a refund for a non-PROCEED outcome", violations)

    def test_internal_terms_fail(self) -> None:
        violations = validate_draft(
            {"body": "The proposed refund is $100.00 for order LF-100216. POLICY_OVERRIDE_ATTEMPT"},
            outcome="PEND",
            refundable_amount=100.0,
            order_id="LF-100216",
        )
        self.assertTrue(any("forbidden internal term" in item for item in violations))

    def test_invalid_request_template_hides_order(self) -> None:
        draft = render_template(
            "INVALID_REQUEST",
            ["ORDER_NOT_FOUND"],
            None,
            None,
            None,
            "Re: my request",
            "customer@example.com",
        )
        self.assertNotRegex(draft["body"], r"LF-\d{6}")
        self.assertNotIn("@", draft["body"])

    def test_bad_llm_draft_falls_back(self) -> None:
        result = build_draft(
            {"ticket_id": "X", "from": "customer@example.com", "subject": "Refund", "body": ""},
            None,
            {"outcome": "PEND", "reason_codes": ["APPROVAL_REQUIRED"], "refundable_amount": 100.0, "approver_role": "SUPPORT_LEAD"},
            "LF-100216",
            llm=BadDraftLLM(),
        )
        self.assertFalse(result.used_llm)
        self.assertTrue(result.violations)
        self.assertIn("$100.00", result.draft["body"])
        self.assertEqual(result.draft["to"], "customer@example.com")

    def test_template_outcomes_never_call_llm(self) -> None:
        result = build_draft(
            {"ticket_id": "X", "from": "customer@example.com", "subject": "Refund", "body": ""},
            None,
            {"outcome": "INVALID_REQUEST", "reason_codes": ["ORDER_NOT_FOUND"], "refundable_amount": None, "approver_role": None},
            None,
            llm=ExplodingLLM(),
        )
        self.assertFalse(result.used_llm)
        self.assertNotRegex(result.draft["body"], r"LF-\d{6}")


if __name__ == "__main__":
    unittest.main()
