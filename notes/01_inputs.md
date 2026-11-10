# 01 - Inputs (data dictionary + contract)

## Data files (`data/`, raw export 2026-11-02, uncleaned)

| file | grain / key | columns | notes |
|---|---|---|---|
| `customers.csv` | 1 row / customer (`customer_id`) | name, email, country, created_at, loyalty_tier (none/silver/gold) | email is "as typed at signup" - all lowercase here; 53 rows |
| `orders.csv` | 1 row / order (`order_id` = `LF-` + 6 digits) | customer_id, order_date, status (DELIVERED/SHIPPED), **`Order Total`**, shipping_fee, currency (USD), payment_method (card/paypal) | 49 rows, 47 unique orders (2 exact dupes) |
| `order_items.csv` | 1 row / order line (`order_id`, `line_no`) | sku, product_name, qty, unit_price | 57 rows; `Order Total` = sum(qty*unit_price), shipping excluded |
| `shipments.csv` | 1 row / order shipment (`shipment_id`, `order_id`) | carrier, tracking_no, status (DELIVERED/IN_TRANSIT/UNAVAILABLE), shipped_at, delivered_at, last_event_at | 47 rows; blanks are meaningful (not delivered / carrier returned nothing) |
| `refunds.csv` | 1 row / issued refund (`refund_id`) | order_id, line_no, amount, status (ISSUED), requested_at, approved_by (auto/approver) | 3 rows; refunds already paid |

## Tickets

| file | content |
|---|---|
| `tickets/support_inbox_export.json` | 30 emails, same shape as API request body (T001-T030) |
| `tickets/eval_seed_labelled.json` | 15 of the above with senior-agent outcome, approval flag, approver role, occasionally amount + reason codes |
| `inbox/public_tickets.json` | 10 further unlabelled tickets in request shape |

Labelled IDs: T001, T002, T003, T004, T005, T006, T008, T011, T013, T014, T018, T019, T020, T021, T026 (T027-T030 are forwards of already-labelled emails).

## Contract (`schema/response_schema.py`, pydantic v2)

Request `Ticket`: `ticket_id`, `received_at`, `channel="email"`, `from_` (alias `from`), `to`, `subject`, `body`, `attachments[]` (`file_name`, `content_type`, `content_base64`).

Response `TicketResponse`:

| field | values |
|---|---|
| `ticket_type` | REFUND, RETURN, ORDER_STATUS, DELIVERY_ISSUE, OTHER |
| `resolved` | `order_id`, `customer_id` (nullable) |
| `outcome` | PROCEED, PROCEED_MODIFIED, PEND, ESCALATE, REJECT, INVALID_REQUEST, UNABLE_TO_VERIFY |
| `reason_codes` | free strings (policy codes supplied in RP-3.2) |
| `checks[]` | `check_id`, `result` (PASS/FAIL/NOT_APPLICABLE/UNABLE_TO_VERIFY), `evidence[]` (`source`,`ref`,`value`), `policy_clause` |
| `human_approval_required` / `approver_role` | SUPPORT_LEAD, TRUST_SAFETY, CARRIER_CLAIMS |
| `draft_reply` | to, subject, body |
| `trace` | `model_calls[]`, `tools_called[]`, `documents_used[]`, `cost_usd` |

Side models: `Decision` (approve/reject + approver) and `LedgerEntry` (`action` REFUND/REPLY/ESCALATION, `amount`, `status` PROPOSED/APPROVED/REJECTED).

Implication: every decision needs a clause-backed evidence trail; `reason_codes` must use the policy's named codes; a refund amount has no top-level field, so carry it in check evidence / ledger.
