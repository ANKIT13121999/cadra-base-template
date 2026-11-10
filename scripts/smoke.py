"""Local smoke test for the Larkfield Home API.

Starts a local uvicorn server with a fresh SQLite run DB, posts all 10 public
tickets, validates every response, approves one pending run twice, and prints
the ledger proof plus median cost/latency.
"""
from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

from schema.response_schema import TicketResponse
from src.prepare import resolve_data_room

REPO = Path(__file__).resolve().parents[1]
DATA_ROOM = resolve_data_room(None)
PUBLIC_TICKETS = DATA_ROOM / "inbox" / "public_tickets.json"
DB_PATH = REPO / "build" / "smoke_runs.sqlite"
SERVER_LOG = REPO / "build" / "smoke_server.log"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            path.unlink()


def _prepare() -> None:
    subprocess.run([sys.executable, "-m", "src.prepare"], cwd=REPO, check=True)


def _wait_for_health(base_url: str, process: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early; see {SERVER_LOG}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.4)
    raise TimeoutError(f"server did not become healthy; see {SERVER_LOG}")


def _validate_run(run: dict) -> None:
    TicketResponse.model_validate(run)
    if not isinstance(run.get("latency_ms"), int):
        raise ValueError("run is missing integer latency_ms")


def main() -> int:
    _prepare()
    _clean_db()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["LARKFIELD_DB_PATH"] = str(DB_PATH)
    env["LARKFIELD_PREPARED_DB"] = str(REPO / "build" / "prepared.db")
    env["PORT"] = str(port)
    log_handle = SERVER_LOG.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "src.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    runs: list[dict] = []
    try:
        _wait_for_health(base_url, process)
        tickets = json.loads(PUBLIC_TICKETS.read_text(encoding="utf-8"))
        with httpx.Client(base_url=base_url, timeout=300) as client:
            for ticket in tickets:
                response = client.post("/tickets", json=ticket)
                response.raise_for_status()
                run = response.json()
                _validate_run(run)
                runs.append(run)
                print(
                    f"{run['ticket_id']:>4}  outcome={run['outcome']:<18} "
                    f"approver={str(run.get('approver_role')):<15} "
                    f"cost=${run['trace']['cost_usd']:.6f}  latency={run['latency_ms']}ms"
                )

            first = tickets[0]
            repeat = client.post("/tickets", json=first)
            repeat.raise_for_status()
            repeat_run = repeat.json()
            if repeat_run["run_id"] != runs[0]["run_id"]:
                raise AssertionError("duplicate ticket_id returned a new run")
            ledger_after_repeat = client.get("/ledger").json()
            first_rows = [row for row in ledger_after_repeat if row["run_id"] == runs[0]["run_id"]]
            if len(first_rows) != 1:
                raise AssertionError("duplicate submit created a second ledger row")
            print(f"idempotency: {repeat_run['ticket_id']} returned run {repeat_run['run_id']} with 1 ledger row")

            pending = next((run for run in runs if run["outcome"] == "PEND"), None)
            if pending is None:
                pending = next((run for run in runs if run.get("human_approval_required")), None)
            if pending is None:
                raise AssertionError("no pending/approval run to exercise the decision endpoint")

            body = {"decision": "approve", "approver": "smoke-test"}
            first_decision = client.post(f"/runs/{pending['run_id']}/decision", json=body)
            first_decision.raise_for_status()
            second_decision = client.post(f"/runs/{pending['run_id']}/decision", json=body)
            second_decision.raise_for_status()
            ledger = client.get("/ledger").json()
            rows = [row for row in ledger if row["run_id"] == pending["run_id"]]
            if len(rows) != 1:
                raise AssertionError(f"expected 1 ledger row for {pending['run_id']}, got {len(rows)}")
            if rows[0]["status"] != "APPROVED":
                raise AssertionError(f"expected APPROVED ledger status, got {rows[0]['status']}")
            print(
                f"double-approve: run={pending['run_id']} ticket={pending['ticket_id']} "
                f"ledger_rows={len(rows)} status={rows[0]['status']} action={rows[0]['action']}"
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log_handle.close()

    costs = [float(run["trace"]["cost_usd"]) for run in runs]
    latencies = [int(run["latency_ms"]) for run in runs]
    print("")
    print(f"schema-valid runs: {len(runs)}/{len(runs)}")
    print(f"median cost: ${statistics.median(costs):.6f} per request")
    print(f"median latency: {int(statistics.median(latencies))} ms per request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
