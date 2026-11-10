"""Run decide() over the 15 seed labels with hand-built facts and report the score."""
from __future__ import annotations

import json
import sys

from src.checks import FAIL, UNABLE_TO_VERIFY, evaluate
from src.prepare import atomic_write_json
from src.repo import PreparedData
from tests.labeled_facts import DB_PATH, REPO, build_labelled_facts, load_labels

KNOWN_LABEL_DISAGREEMENTS = {
    "T011": {
        "fields": {"outcome", "human_approval_required", "approver_role"},
        "clause": "5.1",
        "explanation": (
            "LF-100210 is data-flagged by Task 1 (last_event_at < shipped_at), so the "
            "clause 5.1 movement check is UNABLE_TO_VERIFY rather than LOST_IN_TRANSIT. "
            "The seed label expects an escalation that would require trusting impossible data."
        ),
    }
}

REASON_CLAUSE = {
    "POLICY_OVERRIDE_ATTEMPT": "5.2",
    "ORDER_NOT_FOUND": "4.1",
    "CUSTOMER_MISMATCH": "4.1",
    "CARRIER_STATUS_UNAVAILABLE": "4.2",
    "LOST_IN_TRANSIT": "5.1",
    "OUT_OF_WINDOW": "2.1/2.3/2.6",
    "PARTIAL_LINES": "2.4",
    "ALREADY_REFUNDED": "2.5",
    "APPROVAL_REQUIRED": "3.2",
    "DAMAGE_EXCEPTION": "2.6",
    "MOVEMENT_DATA_UNVERIFIABLE": "5.1",
    "DELIVERY_DATE_UNAVAILABLE": "2.1/2.3",
    "AFFECTED_LINE_UNVERIFIABLE": "2.2",
    "ORDER_AMBIGUOUS": "4.1",
    "IDENTITY_UNVERIFIABLE": "4.1",
    "SCOPE_UNVERIFIABLE": "2.4",
    "ALREADY_PROCESSED": "3.3",
    "UNCLASSIFIED_REQUEST": "intent",
    "OVERRIDE_CHECK_UNAVAILABLE": "5.2",
    "NO_SHIPMENT": "4.2/5.1",
}


def _rate(matched: int, total: int) -> dict[str, float | int]:
    return {"matched": matched, "total": total, "rate": round(matched / total, 4) if total else 0.0}


def build_label_report(data: PreparedData | None = None) -> dict:
    data = data or PreparedData.from_sqlite(DB_PATH)
    facts_by_ticket = build_labelled_facts()
    labels = load_labels()

    ticket_rows: list[dict] = []
    outcome_matched = reason_matched = role_matched = approval_matched = amount_matched = 0
    reason_total = amount_total = 0
    known: list[dict] = []
    unexpected: list[dict] = []

    for ticket_id in labels:
        label = labels[ticket_id]
        facts = facts_by_ticket[ticket_id]
        checks, decision = evaluate(facts, data)

        expected = {
            "outcome": label["expected_outcome"],
            "reason_codes": label.get("expected_reason_codes"),
            "human_approval_required": label["expected_human_approval_required"],
            "approver_role": label["expected_approver_role"],
            "refundable_amount": label.get("expected_amount_usd"),
        }
        actual = {
            "outcome": decision["outcome"],
            "reason_codes": decision["reason_codes"],
            "human_approval_required": decision["human_approval_required"],
            "approver_role": decision["approver_role"],
            "refundable_amount": decision["refundable_amount"],
        }

        mismatch_fields: list[str] = []
        if actual["outcome"] != expected["outcome"]:
            mismatch_fields.append("outcome")
        if expected["reason_codes"] is not None:
            reason_total += 1
            if actual["reason_codes"] == expected["reason_codes"]:
                reason_matched += 1
            else:
                mismatch_fields.append("reason_codes")
        if actual["human_approval_required"] != expected["human_approval_required"]:
            mismatch_fields.append("human_approval_required")
        if actual["approver_role"] != expected["approver_role"]:
            mismatch_fields.append("approver_role")
        if expected["refundable_amount"] is not None:
            amount_total += 1
            if actual["refundable_amount"] == expected["refundable_amount"]:
                amount_matched += 1
            else:
                mismatch_fields.append("refundable_amount")

        if actual["outcome"] == expected["outcome"]:
            outcome_matched += 1
        if actual["approver_role"] == expected["approver_role"]:
            role_matched += 1
        if actual["human_approval_required"] == expected["human_approval_required"]:
            approval_matched += 1

        involved_checks = [
            {
                "check_id": check["check_id"],
                "result": check["result"],
                "policy_clause": check["policy_clause"],
            }
            for check in checks
            if check["result"] in {FAIL, UNABLE_TO_VERIFY}
        ]
        clauses = sorted(
            {check["policy_clause"] for check in involved_checks}
            | {REASON_CLAUSE.get(code, "engine") for code in actual["reason_codes"]}
        )

        row = {
            "ticket_id": ticket_id,
            "ticket_type": facts.ticket_type,
            "expected": expected,
            "actual": actual,
            "mismatch_fields": mismatch_fields,
            "clauses_involved": clauses,
            "checks_involved": involved_checks,
        }
        ticket_rows.append(row)

        if mismatch_fields:
            known_spec = KNOWN_LABEL_DISAGREEMENTS.get(ticket_id)
            is_known = bool(known_spec and set(mismatch_fields) <= known_spec["fields"])
            item = {
                "ticket_id": ticket_id,
                "mismatch_fields": mismatch_fields,
                "expected": expected,
                "actual": actual,
                "clauses_involved": clauses,
                "checks_involved": involved_checks,
            }
            if is_known:
                item["classification"] = "KNOWN_LABEL_DISAGREES_WITH_POLICY"
                item["clause"] = known_spec["clause"]
                item["explanation"] = known_spec["explanation"]
                known.append(item)
            else:
                item["classification"] = "UNEXPECTED"
                unexpected.append(item)

    total = len(labels)
    reason_rate = _rate(reason_matched, reason_total)
    reason_rate["unspecified"] = total - reason_total
    amount_rate = _rate(amount_matched, amount_total)
    amount_rate["unspecified"] = total - amount_total
    report = {
        "generated_by": "tests.label_report",
        "labelled_tickets": total,
        "source": "tickets/eval_seed_labelled.json",
        "hand_built_facts": True,
        "llm_used": False,
        "match_rates": {
            "outcome": _rate(outcome_matched, total),
            "reason_codes": reason_rate,
            "approver_role": _rate(role_matched, total),
            "human_approval_required": _rate(approval_matched, total),
            "refundable_amount": amount_rate,
        },
        "known_label_disagreements": known,
        "unexpected_mismatches": unexpected,
        "tickets": ticket_rows,
    }
    return report


def _result_mark(expected, actual, key: str) -> str:
    if key == "reason_codes":
        return "OK" if expected is None or expected == actual else "MISMATCH"
    return "OK" if expected == actual else "MISMATCH"


def print_report(report: dict) -> None:
    rates = report["match_rates"]
    print("Label replay: 15 seed tickets, hand-built facts, no LLM, no API")
    print(
        "Match rates: outcome {om}/{ot} ({or_:.1f}%) | reason_codes {rm}/{rt} specified ({rr:.1f}%, {ru} unspecified) | "
        "approver_role {am}/{at} ({ar:.1f}%) | human_approval {hm}/{ht} ({hr:.1f}%) | "
        "amount {mm}/{mt} specified ({mr:.1f}%, {mu} unspecified)".format(
            om=rates["outcome"]["matched"],
            ot=rates["outcome"]["total"],
            or_=rates["outcome"]["rate"] * 100,
            rm=rates["reason_codes"]["matched"],
            rt=rates["reason_codes"]["total"],
            rr=rates["reason_codes"]["rate"] * 100,
            ru=rates["reason_codes"].get("unspecified", 0),
            am=rates["approver_role"]["matched"],
            at=rates["approver_role"]["total"],
            ar=rates["approver_role"]["rate"] * 100,
            hm=rates["human_approval_required"]["matched"],
            ht=rates["human_approval_required"]["total"],
            hr=rates["human_approval_required"]["rate"] * 100,
            mm=rates["refundable_amount"]["matched"],
            mt=rates["refundable_amount"]["total"],
            mr=rates["refundable_amount"]["rate"] * 100,
            mu=rates["refundable_amount"].get("unspecified", 0),
        )
    )
    print("")
    print(f"{'ticket':7} {'type':13} {'outcome':20} {'reasons':40} {'approver':16} status")
    for row in report["tickets"]:
        actual = row["actual"]
        outcome_mark = _result_mark(row["expected"]["outcome"], actual["outcome"], "outcome")
        reason_mark = _result_mark(row["expected"]["reason_codes"], actual["reason_codes"], "reason_codes")
        role_mark = _result_mark(row["expected"]["approver_role"], actual["approver_role"], "approver_role")
        status = "OK" if not row["mismatch_fields"] else "MISMATCH"
        print(
            f"{row['ticket_id']:7} {row['ticket_type']:13} {actual['outcome']:20} "
            f"{','.join(actual['reason_codes']) or '-':40} {str(actual['approver_role']):16} "
            f"{status} (outcome={outcome_mark}, reasons={reason_mark}, role={role_mark})"
        )

    print("")
    if not report["unexpected_mismatches"] and not report["known_label_disagreements"]:
        print("No mismatches.")
    for item in report["known_label_disagreements"]:
        checks_text = ", ".join(f"{c['check_id']}={c['result']}" for c in item["checks_involved"]) or "-"
        print(f"MISMATCH {item['ticket_id']} [{item['classification']}] fields={','.join(item['mismatch_fields'])}")
        print(f"  expected: {json.dumps(item['expected'], sort_keys=True)}")
        print(f"  actual:   {json.dumps(item['actual'], sort_keys=True)}")
        print(f"  clause(s): {', '.join(item['clauses_involved']) or '-'}")
        print(f"  checks: {checks_text}")
        print(f"  why: {item['explanation']}")
    for item in report["unexpected_mismatches"]:
        checks_text = ", ".join(f"{c['check_id']}={c['result']}" for c in item["checks_involved"]) or "-"
        print(f"MISMATCH {item['ticket_id']} [UNEXPECTED] fields={','.join(item['mismatch_fields'])}")
        print(f"  expected: {json.dumps(item['expected'], sort_keys=True)}")
        print(f"  actual:   {json.dumps(item['actual'], sort_keys=True)}")
        print(f"  clause(s): {', '.join(item['clauses_involved']) or '-'}")
        print(f"  checks: {checks_text}")

    print("")
    print(
        "Result: outcome {om}/{ot}, reason_codes {rm}/{rt} specified, approver_role {am}/{at}; "
        "{u} unexpected mismatch(es), {k} known label-vs-policy disagreement(s).".format(
            om=rates["outcome"]["matched"],
            ot=rates["outcome"]["total"],
            rm=rates["reason_codes"]["matched"],
            rt=rates["reason_codes"]["total"],
            am=rates["approver_role"]["matched"],
            at=rates["approver_role"]["total"],
            u=len(report["unexpected_mismatches"]),
            k=len(report["known_label_disagreements"]),
        )
    )


def main() -> int:
    if not DB_PATH.is_file():
        print(f"prepared database missing at {DB_PATH}; run `make prepare` first", file=sys.stderr)
        return 2
    report = build_label_report()
    print_report(report)
    out_path = REPO / "build" / "label_report.json"
    atomic_write_json(out_path, report)
    print(f"wrote {out_path}")
    return 0 if not report["unexpected_mismatches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
