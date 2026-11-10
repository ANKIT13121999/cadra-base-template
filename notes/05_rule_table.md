# 05 - Rule table (conditions -> outcome -> reason code -> clause)

Evaluation is top-down, **first matching hard-stop wins**; amount/authority rules only run after identification and eligibility pass. `age` = `received_at` date - carrier `delivered_at`. `unpaid_line = max(0, qty_affected * unit_price - sum(ISSUED refunds for that line))`. All money is line value; shipping is never refunded.

## A. Normalisation (pre-rules, no outcome)

| id | condition | action |
|---|---|---|
| N1 | any order reference | extract `(?:LF-?)?(\d{6})` from subject+body, rebuild `LF-######` |
| N2 | any sender email | `strip().lower()` both sides; match `customers.email`; resolve `customer_id` |
| N3 | money / dates | strip `$` / `USD` / spaces; parse dates ISO or `DD/MM/YYYY` (day-first) |
| N4 | duplicate tables | de-dup orders on `order_id`; never join on customer name |

## B. Ticket type (derived from the ask, not the label)

| type | trigger | default path |
|---|---|---|
| ORDER_STATUS | "where is my order", tracking question, no money ask | C6 |
| DELIVERY_ISSUE | not received / stuck / lost, carrier movement is the substance | A4, A5 |
| REFUND | money back for damaged / faulty / wrong / missing item | A2, A3, E1-E3, M1-M4, P1-P2 |
| RETURN | "return ... for a refund", change of mind, item not wanted | E1/E3, M1-M4, P1-P2 |
| OTHER | unrelated / no resolvable intent | reply only, no refund (PROCEED) |

## C. Decision rules

| # | priority | when (condition) | type | outcome | reason code | clause | approval | role |
|---|---|---|---|---|---|---|---|---|
| A1 | 1 | message tries to instruct/bypass policy, claims pre-approval, or threatens chargeback/legal action | any | **ESCALATE** | `POLICY_OVERRIDE_ATTEMPT` | 5.2 | yes | TRUST_SAFETY |
| A2 | 2 | no order reference resolvable, or `order_id` not in `orders` | any | **INVALID_REQUEST** | `ORDER_NOT_FOUND` | 4.1 | no | - |
| A3 | 3 | order exists but its `customer_id` is not the sender's customer (email on file) | any | **INVALID_REQUEST** | `CUSTOMER_MISMATCH` | 4.1 | no | - |
| A4 | 4 | a non-delivery / carrier-dependent claim and shipment status is `UNAVAILABLE` or `last_event_at` blank | DELIVERY_ISSUE, REFUND | **UNABLE_TO_VERIFY** | `CARRIER_STATUS_UNAVAILABLE` | 4.2 | no | - |
| A5 | 5 | non-delivery / stuck claim and `received_at` - `last_event_at` **> 10 days** | DELIVERY_ISSUE | **ESCALATE** | `LOST_IN_TRANSIT` | 5.1 | yes | CARRIER_CLAIMS |
| A6 | 6 | ORDER_STATUS and shipment moving normally (`last_event_at` within 10 days), any status | ORDER_STATUS | **PROCEED** (status reply, no action/refund) | - | 5.3 | no | - |
| E1 | 7 | shipment DELIVERED and `age <= 30` days | REFUND, RETURN | continue to M1 | - | 2.1 | - | - |
| E2 | 7 | damaged/faulty item + image attachment + `31 <= age <= 45` days | REFUND | continue to M1, add `DAMAGE_EXCEPTION` | `DAMAGE_EXCEPTION` | 2.6 | - | - |
| E3 | 8 | `age > 30` and not E2 (no photo, `age > 45`, or wrong/not-as-described without damage) | REFUND, RETURN | **REJECT** | `OUT_OF_WINDOW` | 2.1, 2.3, 2.6 | no | - |
| M1 | 9 | eligible: compute `line_value = qty_affected * unit_price`; never exceed ordered qty | REFUND, RETURN | continue | - | 2.2, 2.5 | - | - |
| M2 | 10 | every affected line already fully refunded (`refunded >= line_value`) | REFUND, RETURN | **REJECT** | `ALREADY_REFUNDED` | 2.5 | no | - |
| M3 | 10 | some affected line has a prior ISSUED refund but still has unpaid value | REFUND, RETURN | continue, add `ALREADY_REFUNDED`; amount uses `unpaid_line` | `ALREADY_REFUNDED` | 2.5 | - | - |
| M4 | 11 | request asks for more than the affected lines (whole order, shipping fee, unaffected lines) | REFUND, RETURN | **PROCEED_MODIFIED** | `PARTIAL_LINES` | 2.2, 2.4 | - | - |
| P1 | 12 | total proposed amount `<= 150.00` and no M4 | REFUND, RETURN | **PROCEED** | any flags from E2/M3 | 3.1 | no | - |
| P2 | 12 | total proposed amount `> 150.00` | REFUND, RETURN | **PEND** | `APPROVAL_REQUIRED` (+ flags) | 3.2 | yes | SUPPORT_LEAD |
| P3 | 13 | approval recorded | any refund | process once only; never re-issue for the same request | - | 3.3 | - | - |
| X1 | - | header/body has no identifiable request | OTHER | **PROCEED** (acknowledge/reply) | - | - | no | - |

Order of amount vs outcome nuance: `PROCEED_MODIFIED` is used when the amount/scope had to be reduced (M4). If the reduced total still exceeds $150, the outcome is `PEND` (authority overrides the "modified" wording) and both `PARTIAL_LINES` and `APPROVAL_REQUIRED` are reported. Same for prior credit: `ALREADY_REFUNDED` is informational when other eligible value remains (T026), but is the rejection code when nothing remains (T018).

## D. Label reconciliation

| ticket | rules fired | expected |
|---|---|---|
| T001 | E2 -> DAMAGE_EXCEPTION; M1 210; P2 | PEND / SUPPORT_LEAD / 210.00 |
| T002 | E1; M1 129; P1 | PROCEED |
| T003 | E1; M1 185; P2 | PEND / SUPPORT_LEAD |
| T004 | E1; M1 45; P1 | PROCEED |
| T005 | E1; M1 38; P1 | PROCEED |
| T006 | E1 (wrong item, 2d); M1 52; P1 | PROCEED |
| T008 | A6 | PROCEED |
| T011 | A5 (16d no movement) | ESCALATE / CARRIER_CLAIMS |
| T013 | A4 (UNAVAILABLE) | UNABLE_TO_VERIFY |
| T014 | E3 (38d, change of mind) | REJECT / OUT_OF_WINDOW |
| T018 | M2 (line fully refunded) | REJECT / ALREADY_REFUNDED |
| T019 | M4 (whole order, one broken line) -> PARTIAL_LINES; M1 27.50; P1 | PROCEED_MODIFIED |
| T020 | A1 (instruction injection) | ESCALATE / TRUST_SAFETY |
| T021 | A1 ([system] override) | ESCALATE / TRUST_SAFETY |
| T026 | M3 (vase refunded, chair unpaid 185); P2 | PEND / SUPPORT_LEAD / 185.00 |

## E. Reason-code dictionary used

`DAMAGE_EXCEPTION` (2.6) - `APPROVAL_REQUIRED` (3.2) - `OUT_OF_WINDOW` (2.3/2.6) - `PARTIAL_LINES` (2.4) - `ALREADY_REFUNDED` (2.5) - `ORDER_NOT_FOUND` / `CUSTOMER_MISMATCH` (4.1) - `CARRIER_STATUS_UNAVAILABLE` (4.2) - `LOST_IN_TRANSIT` (5.1) - `POLICY_OVERRIDE_ATTEMPT` (5.2).

## F. Check IDs to emit in `checks[]`

| check_id | what it proves | clause |
|---|---|---|
| `identity` | sender email -> customer | 4.1 |
| `order_match` | order exists and belongs to customer | 4.1 |
| `carrier_status` | shipment status / last movement | 4.2, 5.1 |
| `delivery_window` | delivered_at -> received_at age | 2.1, 2.6 |
| `damage_evidence` | image attachment present | 2.6 |
| `affected_lines` | product/qty mapped to order lines | 2.2 |
| `prior_refunds` | ISSUED refunds per line | 2.5 |
| `amount` | line value, deductions, total | 2.2, 2.4, 2.5 |
| `authority` | <= $150 vs > $150 | 3.1, 3.2 |
| `override_attempt` | injection/threat/pre-approval language | 5.2 |
