# 04 - Data quality check

Scope: all five CSVs + the 30-email inbox export. Baseline: 53 customers, 49 order rows (47 unique), 57 order lines, 47 shipments, 3 refunds, 30 tickets.

## 1. Duplicates

| table | finding | action |
|---|---|---|
| `orders.csv` | **2 exact duplicate rows**: LF-100216 (line 5 & 49) and LF-100239 (line 19 & 50). 49 rows -> 47 unique. No conflicting versions. | de-duplicate on `order_id` (keep first) |
| `order_items.csv` | none: 57 unique `(order_id, line_no)` | ok |
| `shipments.csv` | none: 47 unique `order_id` / `shipment_id` | ok |
| `refunds.csv` | none: 3 unique `refund_id`; 1 per order/line | ok |
| `customers.csv` | no duplicate ID or email, **but 23 names are reused** across different customers (e.g. Asha Shah = C10000 + C10030) | never join on name; always `customer_id` / email |
| tickets | 4 forwards duplicate earlier emails: T027=T002, T028=T005, T029=T009, T030=T014 | not an issue if processed independently; do not double-refund by `order_id` |

## 2. Nulls / blanks

| field | count | meaning |
|---|---|---|
| `shipments.delivered_at` | **13 blank** | all 10 IN_TRANSIT + 3 UNAVAILABLE - expected |
| `shipments.last_event_at` | **3 blank** | the 3 UNAVAILABLE rows: LF-100212, LF-100232, LF-100238 - must yield `CARRIER_STATUS_UNAVAILABLE` |
| orders / order_items / customers / refunds | 0 blanks | ok |

Shipment status split: 34 DELIVERED, 10 IN_TRANSIT, 3 UNAVAILABLE.

## 3. Case / format mismatches

- **Email**: 6/30 ticket `from` values contain uppercase and/or leading/trailing spaces (`T001`, `T006`, `T011`, `T016`, `T021`, `T026`), e.g. `"  ASHA.SHAH0@MAIL.EXAMPLE "`. Customer table is all lowercase. Normalise both sides with `strip().lower()`.
- **`Order Total`**: 3 formats - plain `123.00` (16), `USD 123.00` (16), `$123.00` (17). Column name has a space and capital letters. Parse defensively.
- **`order_date`**: 36 ISO vs **13 `DD/MM/YYYY`** (`08/10/2026`, `26/08/2026`, ...). Day-first is proven by `26/08`. Parse with `dayfirst=True` and validate; do not use `order_date` for the refund window anyway.
- **Order ID in tickets**: 5 shapes - `LF-100200`, `LF100201` (no hyphen), `#100202`, `order 100203` / `order order 100219`, bare `100207`. Regex `(?:LF-?)?(\d{6})` then rebuild `LF-{digits}`.
- **Amounts / shipping**: `shipping_fee` is a separate column; `Order Total` = items only (verified below). Never mix them.
- **Timestamps**: ISO `YYYY-MM-DDTHH:MM`, no timezone in CSVs; ticket `received_at` has `+00:00`. Normalise to UTC-aware.

## 4. Orphans / referential integrity

| check | result |
|---|---|
| order_items.order_id -> orders | 0 orphans |
| shipments.order_id -> orders | 0 orphans |
| refunds.order_id -> orders / line -> order_items | 0 orphans |
| orders.customer_id -> customers | 0 orphans |
| orders without items / without shipment | 0 / 0 |
| **missing order** | **LF-100223 is absent** (range 100200-100247 minus 100223); shipment `SHP-5023` is also skipped. Not an orphan, a hole - correct behaviour if referenced: ORDER_NOT_FOUND |

## 5. Refunds already issued

| refund | order | line | amount | line value | status |
|---|---|---|---|---|---|
| RF-900 | LF-100217 | 1 (chair, qty 1) | 185.00 | 185.00 | ISSUED |
| RF-901 | LF-100224 | 1 (vase, qty 1) | 45.00 | 45.00 | ISSUED |
| RF-902 | LF-100243 | 1 (vase, qty 1) | 45.00 | 45.00 | ISSUED |

- No refund exceeds the ordered quantity or the line value; no negative or zero amounts.
- Prior-refund tickets in this inbox: **T018** (LF-100217 fully refunded -> REJECT) and **T026** (LF-100224 vase refunded, chair still unpaid -> deduct).
- `approved_by` is always `auto` here, so no approver history to reuse; LF-100243 has no inbox ticket, but a new one must subtract RF-902.

## 6. Logical / timestamp anomalies

| finding | rows | impact |
|---|---|---|
| `last_event_at` **before** `shipped_at` | LF-100210, LF-100211, LF-100229, LF-100239 | carrier clock/data error; guard day counts (negative/implausible), still >10 days for the two ticket cases |
| order status vs shipment status | 0 mismatches (DELIVERED↔DELIVERED, SHIPPED↔IN_TRANSIT/UNAVAILABLE) | safe to cross-check |
| `Order Total` vs sum(qty x unit_price) | 0 mismatches across 47 orders | order total = item value, shipping excluded |
| ambiguous boundary | T024 LF-100221: last event 10-02 06:00, received 10-12 14:00 = **10d 8h** | strict "> 10 days" -> lost; whole-day flooring changes the outcome |
| date format ambiguity | `08/10/2026` could read Aug 10 vs Oct 8 | use `dayfirst=True`; not used for window anyway |

## 7. Minimum cleaning recipe

1. Load orders, drop exact duplicates by `order_id`.
2. Parse `Order Total` by stripping `$`/`USD`/spaces; parse `order_date` with day-first fallback.
3. Normalise email (`strip().lower()`) on customers and ticket senders.
4. Normalise order refs from tickets to `LF-\d{6}`.
5. Left-join shipments; treat status `UNAVAILABLE` / blank `last_event_at` as unverifiable, not as zero movement.
6. Aggregate ISSUED refunds per `(order_id, line_no)`; compute unpaid affected value as `max(0, qty_affected*unit_price - refunded)`.
7. Compute window from `delivered_at` to `received_at` only when status is DELIVERED.
