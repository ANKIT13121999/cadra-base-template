# Approach

## Problem Summary

Build a support-agent that reads a Larkfield Home customer email and returns a complete, policy-grounded `TicketResponse` (ticket type, resolved order/customer, outcome, reason codes, checks with evidence, approval requirement/role, draft reply, trace).

The hard parts are not the email reading; they are:

- **Policy authority.** `RP-3.2` is the only valid source. The help-centre FAQ is older and contradicts it (14 vs 30-day window, $250 vs $150 auto-approval), and the warehouse document explicitly excludes customer-care rules. Using either would reproduce wrong decisions.
- **Dirty source data.** Raw CRM/OMS/carrier exports contain duplicate orders, three money formats, two date formats, case/whitespace noise in emails, missing carrier statuses, impossible timestamps, and one missing order.
- **Line-level money.** Refunds are capped at the unpaid affected-line value; shipping and unaffected lines are excluded; ISSUED refunds must be subtracted before the $150 authority test.
- **Safety and auditability.** Prompt-injection attempts must be escalated, not obeyed, and every decision needs clause-backed evidence plus a human-approval flag.

The 15 labelled tickets are used as an acceptance suite: a decision is correct only if outcome, approval flag, approver role, amount and reason codes match.

## Technical Approach

### 1. Normalise before reasoning (data layer)

Load all five CSVs once and build in-memory indexes:

- `customers_by_email` after `strip().lower()`; never join on name (23 names are reused).
- `orders_by_id` after de-duplicating on `order_id` (LF-100216 and LF-100239 appear twice).
- `order_lines` keyed by `(order_id, line_no)`, with `line_value = qty * unit_price`.
- `shipments_by_order`, preserving blanks and `UNAVAILABLE` as real signals.
- `refunds_by_line` aggregating ISSUED amounts so prior credit is subtracted once.

Parsing is defensive: money strips `$`/`USD`/spaces, dates accept ISO and `DD/MM/YYYY` (day-first, proven by `26/08/2026`), and ticket order references are reduced to `LF-\d{6}` from variants such as `LF100201`, `#100202`, `order order 100203` and bare `100207`.

### 2. Classify intent and extract entities

Classify each email into `REFUND`, `RETURN`, `ORDER_STATUS`, `DELIVERY_ISSUE` or `OTHER` from the ask, then extract:

- order reference and product/line mentions,
- damage/faulty/wrong/not-as-described cues,
- non-delivery/lost/status cues,
- presence of an image attachment (the policy only requires a supplied photo; no image analysis),
- override/chargeback/legal/pre-approval language.

Order/customer resolution is a hard gate: no order -> `ORDER_NOT_FOUND`; order belonging to another customer -> `CUSTOMER_MISMATCH` (e.g. T023 references Carla's order from Wen's email).

### 3. Ordered, deterministic decision engine

Policy evaluation is a first-match rule table implemented in code, not left to the model:

1. **Safety/trust:** override or injection language -> `ESCALATE` / `POLICY_OVERRIDE_ATTEMPT` / `TRUST_SAFETY`.
2. **Identity:** unresolved order -> `INVALID_REQUEST` / `ORDER_NOT_FOUND`; wrong owner -> `CUSTOMER_MISMATCH`.
3. **Carrier gate:** `UNAVAILABLE` or blank carrier history -> `UNABLE_TO_VERIFY` / `CARRIER_STATUS_UNAVAILABLE`; no movement >10 days -> `ESCALATE` / `LOST_IN_TRANSIT` / `CARRIER_CLAIMS`; normally moving status question -> `PROCEED` reply only.
4. **Eligibility:** delivered age <=30 days, or damaged/faulty with photo at 31-45 days (`DAMAGE_EXCEPTION`); otherwise `REJECT` / `OUT_OF_WINDOW`. Change-of-mind returns never get the damage extension.
5. **Amount:** affected quantity only, capped at ordered quantity; fully refunded line -> `ALREADY_REFUNDED` (reject if nothing left, otherwise deduct and continue); whole-order/shipping asks -> `PROCEED_MODIFIED` / `PARTIAL_LINES`.
6. **Authority:** total proposed amount <= $150 -> `PROCEED`; > $150 -> `PEND` / `APPROVAL_REQUIRED` / `SUPPORT_LEAD`. Approval is not a second refund: processing happens at most once per request.

Every rule emits a `Check` with `check_id`, `PASS`/`FAIL`/`NOT_APPLICABLE`/`UNABLE_TO_VERIFY`, evidence (`source`, `ref`, `value`) and the exact `policy_clause`. Reason codes are only the policy's named codes, so the response is explainable and auditable.

The full table, reconciliation to all 15 labels, and worked cases are in `notes/05_rule_table.md` and `notes/03_labelled_tickets.md`.

### 4. Model's role is narrow

The LLM (via the Cadra/OpenCode provider) is used for:

- paraphrasing the customer's ask into the type/entity schema,
- generating the draft reply from the computed decision, and
- summarising evidence.

It is **not** trusted for arithmetic, identity matching, prior-refund subtraction, policy selection or authority. Those are pure functions over the CSVs, which keeps decisions reproducible and cheap. The `trace` records model calls, tools, documents used (RP-3.2, version 3.2) and cost.

### 5. Audit, idempotency, human-in-the-loop

Each response carries `run_id`, a proposed ledger action (`REFUND`/`REPLY`/`ESCALATION`) with `PROPOSED`/`APPROVED`/`REJECTED`, and the approval role. Pending items are not paid until a `Decision` approves them; replays cannot double-refund the same request (clause 3.3), and prior ISSUED refunds are already netted out.

### 6. Verification

`make test` replays all 15 seed-labelled tickets with hand-built facts (no LLM) and writes `build/label_report.json` plus the unittest results.

- Current match rates: outcome **14/15 (93.3%)**, reason codes **2/2 specified (100%)**, approver role **14/15 (93.3%)**, human-approval flag **14/15 (93.3%)**, amounts **2/2 specified (100%)**.
- The only disagreement is **T011**: LF-100210 is data-flagged (`last_event_at < shipped_at`), so clause 5.1 returns `UNABLE_TO_VERIFY` instead of the label's `ESCALATE / CARRIER_CLAIMS`. Matching the label would require trusting impossible carrier data; the conservative behaviour is deliberate and documented in `docs/stated_rules.md`.
- Regression tests cover LF-100223 (missing order), deduped LF-100216 and a synthetic conflicting duplicate, the UNAVAILABLE shipment, a partly refunded line, and the wrong-sender T023 case.
- Data-quality guards: duplicate orders, mixed formats, blank carrier fields, `last_event_at < shipped_at`.
- `make prepare` rebuilds the prepared database and report deterministically from a clean checkout.

### 7. Service and smoke verification

The runtime is `extract -> resolve -> checks -> decide -> draft -> trace`, exposed through a FastAPI service backed by a file SQLite run/ledger DB. `POST /tickets` is idempotent on `ticket_id` or the deterministic dedupe key; a human decision can update its ledger row once and repeats return the run unchanged. The service only proposes actions; it never issues a refund or sends mail.

Extraction makes one JSON-only LLM call with the email wrapped as untrusted data, and always runs the deterministic regex/injection/forwarded-sender backup. Drafting uses deterministic templates for `REJECT`, `INVALID_REQUEST` and `UNABLE_TO_VERIFY`; the other outcomes get one small LLM draft that is rejected in favour of the template if an amount, order id, refund promise or forbidden internal term fails validation. Each run records real model/tokens/cost plus `latency_ms`.

`make smoke` posts all 10 public tickets to a local server: **10/10 were schema-valid**, the idempotent repeat produced one run/ledger row, and double-approving one pending run left exactly one ledger row (`APPROVED`). Latest measured medians: **$0.001124 cost** and **3,887 ms latency** per request.

## AI Tool Usage

Cadra/OpenCode was used to inspect the data room, summarise the policy hierarchy, profile the CSV exports and derive the rule table; the 15 labels were then used to validate each derived rule rather than to memorise outcomes. Key iterations were: rejecting the FAQ's conflicting thresholds, adding tolerant order-ID/email normalisation, adding the line-level prior-refund calculation, and separating the deterministic decision engine from the reply-writing model. Findings are captured in `notes/` so every rule traces back to a clause and a piece of evidence.

## Trade-offs & Limitations

- **Deterministic rules over LLM judgement.** Safer and testable, but intent and line selection outside the observed phrasings may need richer NLP; product names are mapped heuristically.
- **Small labelled set.** 15 labels cover the main branches but not all combinations (e.g. out-of-window damaged without photo, partial refunds plus escalation). The rule table encodes the policy, so untested combinations follow the clause rather than a fitted pattern.
- **Boundary ambiguity.** T024 is 10 days 8 hours, so strict ">10 days" classifies it as lost; the brief/labels do not pin this down. Timestamps lack offsets and four `last_event_at` values precede `shipped_at`, so the engine flags rather than silently trusts them.
- **No image-content analysis.** RP-3.2 explicitly accepts a supplied image; this is a policy choice, not a technical shortcut. Abuse beyond an attachment check is not detectable here.
- **Missing LF-100223.** It is absent from every table; a request for it returns `ORDER_NOT_FOUND`, which is correct but means no shipment evidence exists.
- **FAQ deliberately ignored.** It remains in the data room to test whether the agent follows the authoritative, newer policy; a user-facing reply should explain the RP-3.2 window rather than quote the FAQ.
- With more time: add a proper rule-expression engine, persist the ledger, expand evaluation with synthetic edge cases, and add confidence-based escalation for ambiguous line selection.
