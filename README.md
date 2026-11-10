# F1 Capstone Template

Starter repository for the Cadra F1 walking-skeleton capstone. Use OpenCode with the Cadra provider to build your solution, document your approach, and submit a public GitHub repo for evaluation.

## Prerequisites

- Python 3.11+
- [OpenCode](https://opencode.ai) ≥ 1.17.0
- A Cadra JWT (`CADRA_TOKEN`) from the F1 Setup page
- Your Cadra proxy URL (from the F1 Setup page)

## Setup

1. Clone this repo (or use it as a GitHub template).
2. Set the environment variables (both values come from the F1 Setup page):
   ```bash
   export CADRA_PROXY_URL=<your-proxy-url>   # e.g. https://your-proxy.example.com/v1
   export CADRA_TOKEN=<your-cadra-jwt>
   ```
   `opencode.json` reads both via `{env:…}` — no file edits needed.
3. Install OpenCode if not already installed (see [opencode.ai](https://opencode.ai)).
4. Run OpenCode in this directory:
   ```bash
   opencode
   ```
5. Complete `APPROACH.md` and implement your solution in `src/` (start with `src/solution.py`).
6. Push your work to a **public** GitHub repository.
7. Submit your repo URL on the F1 demo page.

## Data preparation

One command, reproducible and safe to re-run:

```bash
python -m src.prepare
```

It reads the raw data room without modifying it and writes:

- `build/prepared.db` - cleaned, indexed SQLite tables (orders, items, shipments, refunds, customers, tickets, duplicate-ticket flags, eval labels)
- `build/prep_report.json` - exact rows in/out, every fix applied and every flag raised

The data room is auto-detected (current directory, repo root, then its parent). Override with `--data-room <path>` and `--out-dir <path>` if needed.

## Make targets

```bash
make prepare   # build/prepared.db + build/prep_report.json
make test      # prepare, label report, and unit tests
make run       # start the API on PORT (default 8000)
make smoke     # post all 10 public tickets locally and verify the decision flow
```

`make test` replays `decide()` against 15 hand-built ticket fact sets, prints match rates and mismatches, writes `build/label_report.json`, then runs the unit tests.

## API service

```bash
python -m uvicorn src.api:app --host 0.0.0.0 --port $PORT
# equivalent entry point:
python -m src.solution
```

Environment:

- `LARKFIELD_DB_PATH` - run/ledger SQLite file (default `build/runs.db`)
- `LARKFIELD_PREPARED_DB` - prepared data SQLite file (default `build/prepared.db`)
- `PORT` - API port (default `8000`)
- `CADRA_PROXY_URL`, `CADRA_TOKEN` - model endpoint and token; local dev falls back to `opencode.json`
- `CADRA_MODEL` - model name (default `cadra`)
- `PRICE_IN_PER_MTOK` / `PRICE_OUT_PER_MTOK` - token prices for `measured_cost_usd` (defaults 0.60 / 3.00)

Endpoints: `GET /health`, `POST /tickets`, `GET /runs/{id}`, `POST /runs/{id}/decision`, `GET /ledger`, `GET /debug/llm`.

`POST /tickets` is idempotent on `ticket_id` or the deterministic dedupe key: a repeat returns the existing run and does not create a second ledger row. A decision can be applied once to a pending approval run; repeats/conflicts return the run unchanged. The service only proposes decisions - it never issues a refund or sends email.

Added run field: **`latency_ms`** (integer, end-to-end pipeline time). Trace model calls record the real returned model name and token usage; `measured_cost_usd` is computed from `PRICE_IN_PER_MTOK`/`PRICE_OUT_PER_MTOK` (defaults 0.60 / 3.00). `trace.llm_error` records a short reason when an LLM call is skipped or fails, and `GET /debug/llm` performs one tiny call.

## Checks engine

Pure Python, no LLM or network. It reads `build/prepared.db` and returns one
clause check per RP-3.2 rule plus the ordered decision:

```python
from src.repo import PreparedData
from src.facts import TicketFacts, AffectedItem
from src.checks import evaluate

data = PreparedData.from_sqlite("build/prepared.db")
facts = TicketFacts(
    ticket_id="T001",
    received_at_utc="2026-10-01T09:00:00Z",
    customer_email="asha.shah0@mail.example",
    ticket_type="REFUND",
    order_ref="LF-100200",
    affected_items=(AffectedItem(product_name="Round wall mirror", quantity=1),),
    damaged_or_faulty=True,
    wrong_or_not_as_described=False,
    non_delivery_claim=False,
    whole_order_requested=False,
    shipping_refund_requested=False,
    override_attempt=False,
    has_photo_attachment=True,
    already_processed=False,
)
checks, decision = evaluate(facts, data)
```

Every gap the policy does not resolve is recorded in `docs/stated_rules.md`.
Run the checks suite with `python -m unittest discover -s tests -v`.

## Python environment (optional)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Project layout

```
.
├── opencode.json    # Cadra provider config (reads CADRA_PROXY_URL + CADRA_TOKEN from env)
├── APPROACH.md      # Your written approach (required for submission)
├── src/
│   └── solution.py  # Your solution code
├── requirements.txt
└── README.md
```

## Not built

- **Persistent ledger.** The SQLite run/ledger DB (`DB_PATH`, default `build/runs.db`) sits on Render's ephemeral filesystem on the free tier, so runs and approvals reset on restart. Production needs a managed database or persistent volume.
- **Outbound actions.** The service never sends email or issues refunds; drafts are proposals only.
- **Operational hardening.** No auth, rate limiting, monitoring, or backup/restore for the ledger yet.
