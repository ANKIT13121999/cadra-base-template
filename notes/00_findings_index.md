# 00 - Findings index

Inputs reviewed (data room, export date 2026-11-02):

- `DATA_DICTIONARY.md`, `schema/response_schema.py`
- `policies/refund_policy_RP-3.2.md`, `policies/help_centre_faq_v2.md`, `policies/warehouse_picking_WH-PICK-1.4.md`
- `tickets/eval_seed_labelled.json` (15 labels) + `tickets/support_inbox_export.json` (30 emails)
- `data/*.csv` (customers, orders, order_items, shipments, refunds)

Files in this folder:

| file | content |
|---|---|
| `01_inputs.md` | data dictionary + API contract summary |
| `02_policies.md` | authoritative vs superseded docs, clause map |
| `03_labelled_tickets.md` | the 15 labels, derived logic per ticket |
| `04_data_quality.md` | duplicates, nulls, case/format, orphans, prior refunds |
| `05_rule_table.md` | conditions -> outcome -> reason code -> clause (+ role) |

Top findings:

1. **RP-3.2 is the only authority.** Help-centre FAQ is older (2025) and contradicts it (14 vs 30 days; $250 auto vs $150 approval). Warehouse doc is internal and explicitly out of scope.
2. **Identity is the first hard gate.** All 30 senders resolve after trim+lowercase; order IDs need tolerant parsing (`LF100201`, `#100202`, `order order 100203`, bare `100207`).
3. **Amounts are line-value only.** Never shipping, never whole order for one damaged line; subtract refunds already ISSUED per line.
4. **Two hard stops produce no refund:** UNAVAILABLE carrier status -> `UNABLE_TO_VERIFY`; unverifiable/mismatched order -> `INVALID_REQUEST`.
5. **Authority is a single $150 threshold per request**, not per line: >150 -> `PEND` + `SUPPORT_LEAD`. Lost-in-transit -> `CARRIER_CLAIMS`; override/injection -> `TRUST_SAFETY`.
6. **Data is dirty in known ways:** 2 duplicate orders, 3 mixed `Order Total` formats, 13 non-ISO dates, 6 emails with case/space noise, 4 shipments with `last_event_at < shipped_at`, 3 `UNAVAILABLE`, and LF-100223 is entirely missing.
