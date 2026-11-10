from __future__ import annotations

import unittest

from src.checks import (
    FAIL,
    UNABLE_TO_VERIFY,
    check_2_6_damage_exception,
    check_5_2_override_attempt,
    evaluate,
)
from src.facts import AffectedItem, TicketFacts
from src.repo import PreparedData
from tests.label_report import build_label_report
from tests.labeled_facts import DB_PATH, build_labelled_facts, load_labels, load_source_tickets


def item(name: str, quantity: int = 1) -> AffectedItem:
    return AffectedItem(product_name=name, quantity=quantity)


def facts(
    ticket_id: str,
    ticket_type: str,
    order_ref: str | None,
    items: list[AffectedItem] | tuple[AffectedItem, ...] = (),
    received_at_utc: str = "2026-10-05T10:00:00Z",
    customer_email: str = "owner@example.com",
    **overrides,
) -> TicketFacts:
    payload = {
        "ticket_id": ticket_id,
        "received_at_utc": received_at_utc,
        "customer_email": customer_email,
        "ticket_type": ticket_type,
        "order_ref": order_ref,
        "affected_items": tuple(items),
        "damaged_or_faulty": False,
        "wrong_or_not_as_described": False,
        "non_delivery_claim": False,
        "whole_order_requested": False,
        "shipping_refund_requested": False,
        "override_attempt": False,
        "has_photo_attachment": False,
        "already_processed": False,
    }
    payload.update(overrides)
    return TicketFacts(**payload)


def synthetic_data(*, ambiguous: bool = False, refunded_usd: float | None = None) -> PreparedData:
    customers = [
        {
            "customer_id": "C900",
            "name": "Test Owner",
            "email_raw": "owner@example.com",
            "email_norm": "owner@example.com",
            "country": "US",
            "created_at_utc": "2026-01-01T00:00:00Z",
            "loyalty_tier": "none",
        }
    ]
    orders = [
        {
            "order_id": "LF-100999",
            "customer_id": "C900",
            "order_date_utc": "2026-09-29T00:00:00Z",
            "status": "DELIVERED",
            "order_total_usd": 100.0,
            "shipping_fee_usd": 0.0,
            "currency": "USD",
            "payment_method": "card",
            "data_quality_status": "AMBIGUOUS" if ambiguous else "OK",
            "ambiguous_fields": '["order_total_usd"]' if ambiguous else None,
            "source_rows": 2 if ambiguous else 1,
        }
    ]
    order_items = [
        {
            "order_id": "LF-100999",
            "line_no": 1,
            "sku": "SKU-TEST",
            "product_name": "Test item",
            "qty": 1,
            "unit_price_usd": 100.0,
            "line_value_usd": 100.0,
        }
    ]
    shipments = [
        {
            "shipment_id": "SHP-TEST",
            "order_id": "LF-100999",
            "carrier": "TestCarrier",
            "tracking_no": "TRK-TEST",
            "status": "DELIVERED",
            "shipped_at_utc": "2026-09-30T10:00:00Z",
            "delivered_at_utc": "2026-10-01T10:00:00Z",
            "last_event_at_utc": "2026-10-01T10:00:00Z",
            "carrier_status_usable": 1,
            "movement_check_usable": 1,
            "flag_codes": "[]",
            "flag_detail": None,
        }
    ]
    refunds = []
    if refunded_usd is not None:
        refunds.append(
            {
                "refund_id": "RF-TEST",
                "order_id": "LF-100999",
                "line_no": 1,
                "amount_usd": refunded_usd,
                "status": "ISSUED",
                "requested_at_utc": "2026-10-02T10:00:00Z",
                "approved_by": "auto",
            }
        )
    return PreparedData(customers, orders, order_items, shipments, refunds)


def source_facts(ticket_id: str, **overrides) -> TicketFacts:
    source = load_source_tickets()[ticket_id]
    payload = {
        "ticket_id": ticket_id,
        "received_at_utc": source["received_at"],
        "customer_email": source["from"],
        "ticket_type": "REFUND",
        "order_ref": None,
        "affected_items": (),
        "damaged_or_faulty": False,
        "wrong_or_not_as_described": False,
        "non_delivery_claim": False,
        "whole_order_requested": False,
        "shipping_refund_requested": False,
        "override_attempt": False,
        "has_photo_attachment": False,
        "already_processed": False,
    }
    payload.update(overrides)
    return TicketFacts(**payload)


@unittest.skipUnless(DB_PATH.is_file(), "run `python -m src.prepare` first")
class ChecksEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = PreparedData.from_sqlite(DB_PATH)
        cls.labelled_facts = build_labelled_facts()
        cls.labels = load_labels()

    def test_label_report_has_only_known_disagreement(self) -> None:
        report = build_label_report(self.data)
        self.assertEqual(report["unexpected_mismatches"], [])
        self.assertEqual([item["ticket_id"] for item in report["known_label_disagreements"]], ["T011"])
        rates = report["match_rates"]
        self.assertEqual((rates["outcome"]["matched"], rates["outcome"]["total"]), (14, 15))
        self.assertEqual((rates["reason_codes"]["matched"], rates["reason_codes"]["total"]), (2, 2))
        self.assertEqual((rates["approver_role"]["matched"], rates["approver_role"]["total"]), (14, 15))
        self.assertEqual((rates["refundable_amount"]["matched"], rates["refundable_amount"]["total"]), (2, 2))
        self.assertEqual(report["known_label_disagreements"][0]["clause"], "5.1")

    def test_every_labelled_ticket_has_complete_checks(self) -> None:
        for ticket_id, label in self.labels.items():
            with self.subTest(ticket=ticket_id):
                checks, decision = evaluate(self.labelled_facts[ticket_id], self.data)
                self.assertEqual(len(checks), 14)
                for check in checks:
                    self.assertEqual(set(check) - {"details"}, {"check_id", "result", "policy_clause", "evidence"})
                    self.assertIn(check["result"], {"PASS", "FAIL", "NOT_APPLICABLE", "UNABLE_TO_VERIFY"})
                    self.assertTrue(check["policy_clause"])
                    self.assertIsInstance(check["evidence"], list)
                self.assertEqual(
                    set(decision),
                    {"outcome", "reason_codes", "human_approval_required", "approver_role", "refundable_amount"},
                )
                if ticket_id != "T011":
                    self.assertEqual(decision["outcome"], label["expected_outcome"])
                    self.assertEqual(decision["human_approval_required"], label["expected_human_approval_required"])
                    self.assertEqual(decision["approver_role"], label["expected_approver_role"])

    def test_flagged_movement_is_unverifiable(self) -> None:
        checks, decision = evaluate(self.labelled_facts["T011"], self.data)
        movement = next(c for c in checks if c["check_id"] == "5.1_lost_in_transit")
        self.assertEqual(movement["result"], UNABLE_TO_VERIFY)
        self.assertEqual(decision["outcome"], UNABLE_TO_VERIFY)
        self.assertEqual(decision["reason_codes"], ["MOVEMENT_DATA_UNVERIFIABLE"])
        self.assertFalse(decision["human_approval_required"])

    def test_missing_order_id_gap(self) -> None:
        missing = source_facts(
            "T022",
            order_ref="LF-100223",
            affected_items=(item("Glass vase"),),
            damaged_or_faulty=True,
            has_photo_attachment=True,
        )
        checks, decision = evaluate(missing, self.data)
        identity = next(c for c in checks if c["check_id"] == "4.1_identity")
        self.assertEqual(identity["result"], FAIL)
        self.assertEqual(identity["policy_clause"], "4.1")
        self.assertEqual(decision["outcome"], "INVALID_REQUEST")
        self.assertEqual(decision["reason_codes"], ["ORDER_NOT_FOUND"])

    def test_duplicate_order_is_deduped_without_double_counting(self) -> None:
        record = self.data.order("LF-100216")
        self.assertIsNotNone(record)
        self.assertEqual(record["source_rows"], 2)
        self.assertEqual(record["data_quality_status"], "OK")
        duplicate = source_facts(
            "T017",
            received_at_utc="2026-08-28T10:00:00Z",
            order_ref="LF-100216",
            affected_items=(item("Side table"),),
            damaged_or_faulty=True,
            has_photo_attachment=True,
        )
        checks, decision = evaluate(duplicate, self.data)
        identity = next(c for c in checks if c["check_id"] == "4.1_identity")
        self.assertEqual(identity["result"], "PASS")
        self.assertEqual(decision["refundable_amount"], 310.0)
        self.assertEqual(decision["outcome"], "PEND")
        self.assertEqual(decision["approver_role"], "SUPPORT_LEAD")

    def test_conflicting_duplicate_order_is_unverifiable(self) -> None:
        data = synthetic_data(ambiguous=True)
        ambiguous = facts(
            "SYN-AMBIGUOUS",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            damaged_or_faulty=True,
            has_photo_attachment=True,
        )
        checks, decision = evaluate(ambiguous, data)
        identity = next(c for c in checks if c["check_id"] == "4.1_identity")
        self.assertEqual(identity["result"], UNABLE_TO_VERIFY)
        self.assertEqual(identity["details"]["failure_reason"], "ORDER_AMBIGUOUS")
        self.assertEqual(decision["outcome"], "UNABLE_TO_VERIFY")
        self.assertEqual(decision["reason_codes"], ["ORDER_AMBIGUOUS"])

    def test_unavailable_shipment(self) -> None:
        checks, decision = evaluate(self.labelled_facts["T013"], self.data)
        carrier = next(c for c in checks if c["check_id"] == "4.2_carrier_status")
        self.assertEqual(carrier["result"], UNABLE_TO_VERIFY)
        self.assertEqual(carrier["policy_clause"], "4.2")
        self.assertEqual(decision["outcome"], UNABLE_TO_VERIFY)
        self.assertEqual(decision["reason_codes"], ["CARRIER_STATUS_UNAVAILABLE"])

    def test_partly_refunded_line(self) -> None:
        data = synthetic_data(refunded_usd=40.0)
        partial = facts(
            "SYN-PARTIAL",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            damaged_or_faulty=True,
            has_photo_attachment=True,
        )
        checks, decision = evaluate(partial, data)
        prior = next(c for c in checks if c["check_id"] == "2.5_prior_refunds")
        self.assertEqual(prior["result"], "PASS")
        self.assertEqual(prior["policy_clause"], "2.5")
        self.assertEqual(prior["details"]["refundable_amount"], 60.0)
        self.assertTrue(prior["details"]["has_prior_refund"])
        self.assertEqual(decision["outcome"], "PROCEED")
        self.assertEqual(decision["reason_codes"], ["ALREADY_REFUNDED"])
        self.assertEqual(decision["refundable_amount"], 60.0)

    def test_wrong_sender_order(self) -> None:
        wrong_sender = source_facts(
            "T023",
            order_ref="LF-100202",
            affected_items=(item("Oak dining chair"),),
            damaged_or_faulty=True,
        )
        checks, decision = evaluate(wrong_sender, self.data)
        identity = next(c for c in checks if c["check_id"] == "4.1_identity")
        self.assertEqual(identity["result"], FAIL)
        self.assertEqual(identity["details"]["failure_reason"], "CUSTOMER_MISMATCH")
        self.assertEqual(decision["outcome"], "INVALID_REQUEST")
        self.assertEqual(decision["reason_codes"], ["CUSTOMER_MISMATCH"])

    def test_boundary_more_than_ten_days_escalates(self) -> None:
        boundary = facts(
            "T024",
            "DELIVERY_ISSUE",
            "LF-100221",
            received_at_utc="2026-10-12T14:00:00Z",
            customer_email="xavier.murphy23@mail.example",
            non_delivery_claim=True,
        )
        checks, decision = evaluate(boundary, self.data)
        movement = next(c for c in checks if c["check_id"] == "5.1_lost_in_transit")
        self.assertEqual(movement["result"], FAIL)
        self.assertGreater(movement["details"]["elapsed_days"], 10.0)
        self.assertEqual(decision["outcome"], "ESCALATE")
        self.assertEqual(decision["approver_role"], "CARRIER_CLAIMS")

    def test_missing_facts_never_pass(self) -> None:
        missing_damage = facts(
            "SYN-MISSING",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            damaged_or_faulty=None,
            has_photo_attachment=True,
        )
        result = check_2_6_damage_exception(missing_damage, synthetic_data())
        self.assertEqual(result["result"], UNABLE_TO_VERIFY)

        missing_override = facts(
            "SYN-MISSING",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            damaged_or_faulty=True,
            has_photo_attachment=True,
            override_attempt=None,
        )
        override = check_5_2_override_attempt(missing_override, synthetic_data())
        self.assertEqual(override["result"], UNABLE_TO_VERIFY)
        _, decision = evaluate(missing_override, synthetic_data())
        self.assertEqual(decision["outcome"], UNABLE_TO_VERIFY)
        self.assertEqual(decision["reason_codes"], ["OVERRIDE_CHECK_UNAVAILABLE"])

    def test_authority_boundary(self) -> None:
        low = facts(
            "SYN-LOW",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            damaged_or_faulty=True,
            has_photo_attachment=True,
            received_at_utc="2026-10-02T10:00:00Z",
        )
        data_low = synthetic_data()
        data_low.orders["LF-100999"]["order_total_usd"] = 100.0
        data_low.lines_by_order["LF-100999"][0]["unit_price_usd"] = 100.0
        data_low.lines_by_order["LF-100999"][0]["line_value_usd"] = 100.0
        _, decision_low = evaluate(low, data_low)
        self.assertEqual(decision_low["outcome"], "PROCEED")
        self.assertFalse(decision_low["human_approval_required"])

        high = facts(
            "SYN-HIGH",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            damaged_or_faulty=True,
            has_photo_attachment=True,
            received_at_utc="2026-10-02T10:00:00Z",
        )
        data_high = synthetic_data()
        data_high.lines_by_order["LF-100999"][0]["unit_price_usd"] = 200.0
        data_high.lines_by_order["LF-100999"][0]["line_value_usd"] = 200.0
        _, decision_high = evaluate(high, data_high)
        self.assertEqual(decision_high["outcome"], "PEND")
        self.assertTrue(decision_high["human_approval_required"])
        self.assertEqual(decision_high["approver_role"], "SUPPORT_LEAD")

    def test_identity_checks_use_normalised_email(self) -> None:
        spaced = facts(
            "SYN-EMAIL",
            "REFUND",
            "LF-100999",
            [item("Test item")],
            customer_email="  OWNER@EXAMPLE.COM ",
            damaged_or_faulty=True,
            has_photo_attachment=True,
        )
        checks, _ = evaluate(spaced, synthetic_data())
        identity = next(c for c in checks if c["check_id"] == "4.1_identity")
        self.assertEqual(identity["result"], "PASS")


if __name__ == "__main__":
    unittest.main()
