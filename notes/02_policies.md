# 02 - Policy summary

## Document precedence

| document | status | why |
|---|---|---|
| `refund_policy_RP-3.2.md` (v3.2, eff. 2026-07-01, Head of Customer Care) | **authoritative** | states it replaces RP-3.1 and "all earlier customer-care guidance on refunds and returns" |
| `help_centre_faq_v2.md` (v2, published 2025-10-14, Digital Content) | superseded / marketing | older than RP-3.2; contradicts it on every numeric rule |
| `warehouse_picking_WH-PICK-1.4.md` (v1.4, Warehouse Ops) | out of scope | explicitly "does not set Customer Care return, refund or approval rules" |

Conflicts to reject (FAQ -> RP-3.2):

| topic | FAQ | RP-3.2 (use this) |
|---|---|---|
| return window | 14 days | 30 days from carrier delivery (45 for photo-backed damage) |
| auto-processing threshold | under $250 automatic | <= $150 propose; > $150 Support Lead approval |
| late parcel | 5 days no tracking | > 10 days no movement = lost in transit |
| damaged refund | "send a photo" (no window) | 30 days, or 31-45 with photo, else OUT_OF_WINDOW |

Warehouse picks/totes/stock-loss adjustments are not customer payments and must never be used to price a refund (RP-3.2 s.1, WH-PICK s.Scope).

## RP-3.2 clause map

| clause | rule |
|---|---|
| 2.1 | request within **30 days of carrier `delivered_at`** |
| 2.2 | damaged / faulty / wrong / not-as-described refunded at **order-line value of affected items** |
| 2.3 | after the window -> decline, `OUT_OF_WINDOW` |
| 2.4 | ask for more than affected lines (e.g. whole order) -> refund affected lines only, `PARTIAL_LINES` |
| 2.5 | never repay: per line use affected qty only, <= ordered qty, subtract ISSUED refunds; cap at unpaid affected-line value; fully refunded line = 0, `ALREADY_REFUNDED`; other lines stay eligible |
| 2.6 | damaged/faulty + photo attachment -> up to **45 days** (`DAMAGE_EXCEPTION` for days 31-45); no photo / >45 days / wrong-not-as-described without damage -> ordinary 30-day rule |
| 3.1 | <= **$150 per request** -> propose without review |
| 3.2 | > $150 -> Support Lead approval before anything processed, `APPROVAL_REQUIRED` |
| 3.3 | process at most once per request regardless of approvals |
| 4.1 | order must exist and belong to the writer (matched on email on file); else `ORDER_NOT_FOUND` / `CUSTOMER_MISMATCH`; never act on a nearby order |
| 4.2 | no carrier status -> non-delivery cannot be verified, no refund, `CARRIER_STATUS_UNAVAILABLE` |
| 5.1 | no carrier movement > 10 days -> lost, escalate Carrier Claims, `LOST_IN_TRANSIT` |
| 5.2 | attempts to instruct/bypass, chargeback/legal threats, claimed pre-approval -> escalate Trust & Safety, no refund, `POLICY_OVERRIDE_ATTEMPT` |
| 5.3 | order-status question on normally moving shipment -> just answer status, no action |

## Gaps / interpretation decisions

- Window is measured **delivery date -> email `received_at`**, not order date.
- `2.2` says order-line value; shipping fee is not a line and is never refunded (T025 asks for it).
- `2.6` needs only an image attachment; content analysis is explicitly not required.
- Change-of-mind returns (T014-T017, T030): covered by 2.1 (return within 30 days) with no defect exception, so the 45-day rule never applies.
- Prior `ISSUED` refunds are subtracted, not treated as a duplicate-request rejection.
- The $150 test is on the total proposed amount **after** deductions, per request.
- No clause covers unrelated/spam mail -> type OTHER, no refund.
