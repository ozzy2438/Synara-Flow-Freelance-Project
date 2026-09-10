# Synara — silent stockout early warning

Retail stockouts rarely show up as a red “zero” on an inventory screen in time for a CFO to care. They show up as **contribution margin that will not be sold** in the next 48 hours, and as **margin you can still recover** if you expedite a PO now versus margin already lost to supplier lead time.

This repo is an end-to-end decision engine for that question:

- Postgres is the OLTP system of record (orders, inventory, purchase orders).
- A **transactional outbox** writes domain events in the **same transaction** as the inventory mutation.
- A **dedicated worker** (not FastAPI `BackgroundTasks`) claims rows with `FOR UPDATE SKIP LOCKED` and applies them **idempotently** to a file-backed DuckDB OLAP sink.
- DuckDB aggregates **48-hour sales velocity** (spike-sensitive) and a 7-day baseline for contrast.
- A Streamlit cockpit is an **API client**. It does not open DuckDB. That avoids the “in-memory DuckDB shared by two processes” trap.

This is **not** a Modern Data Stack. There is no warehouse, dbt, or orchestrator. DuckDB is in-process OLAP for what-if math on 50 SKUs. Calling that “MDS mastery” would be a lie; calling it the right-sized tool is the point.

## What a manager sees

| Metric | Meaning |
| --- | --- |
| At-risk contribution margin | Units projected to stock out in 48h × unit price × margin rate |
| Recoverable if PO placed now | Margin a **24h expedite PO** can still cover inside the horizon |
| Unrecoverable | Demand that hits **before** that PO can arrive |
| At-risk gross (list price) | Shown in the API payload, not as the headline — it overstates economics |

An emergency PO is persisted (`purchase_orders` + `inventory.on_order` + outbox event). The next 48h walk includes that inbound receipt. The button is not decorative.

## Architecture

```
POST /api/orders
        │
        ▼
┌─────────────────── Postgres TX ───────────────────┐
│  lock inventory (FOR UPDATE)                       │
│  reject oversell                                   │
│  insert orders                                     │
│  insert outbox_events (order_placed, maybe ROP)    │
└────────────────────────────────────────────────────┘
        │
        ▼
  worker (SKIP LOCKED)  ──►  DuckDB file (OLAP sink)
        │
        ▼
GET /api/alerts  ──►  velocity in DuckDB + hourly walk in domain
        │
        ▼
Streamlit cockpit (HTTP only)
```

```
src/synara/
  domain/           entities, ROP, revenue split
  application/      place order, seed, simulate, emergency PO
  infrastructure/   SQLAlchemy, outbox worker, DuckDB warehouse
  api/              FastAPI
  cockpit/          Streamlit
  data/             deterministic 50-SKU generator (seed=42)
tests/              outbox rollback, concurrency, simulation, API
```

## Quick start (Docker)

```bash
docker compose up --build
```

1. Open http://localhost:8501
2. Sidebar: **Load / reset synthetic world**
3. Five high-runners have a 36h demand spike sized so they stock out inside 48h
4. Place an emergency PO and watch shortfall / recoverable margin move

API: http://localhost:8000/docs  
Header: `X-API-Key: dev-synara-key` (override with `API_KEY`)

## Quick start (local)

Postgres 16, Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_URL=postgresql+psycopg://synara:synara@localhost:5432/synara
export DUCKDB_PATH=./data/synara.duckdb
export API_KEY=dev-synara-key
uvicorn synara.api.app:app --reload --port 8000
# other terminal
python -m synara.infrastructure.worker
# other terminal
API_BASE_URL=http://localhost:8000 API_KEY=dev-synara-key \
  streamlit run src/synara/cockpit/app.py
```

```bash
export TEST_DATABASE_URL=postgresql+psycopg://synara:synara@localhost:5432/synara_test
pytest -q
```

## API

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/seed` | 50 SKUs, 7 days of orders, injected spikes |
| POST | `/api/orders` | Decrement stock + outbox in one TX; 409 on oversell |
| GET | `/api/alerts` | 48h walk + margin split + pending outbox depth |
| POST | `/api/simulate` | Same walk with demand × and lead-time delta |
| POST | `/api/replenish` | Persist expedite PO (24h) and re-run via alerts |
| GET | `/api/health` | Liveness (no API key) |

Mutating routes require `X-API-Key`. This is a demo lock, not a product auth system.

## Design tradeoffs (read this before a hiring conversation)

**Outbox.** The dual-write being solved is Postgres OLTP vs DuckDB OLAP, not Postgres vs Kafka. Swap the worker’s sink for a broker without changing `place_order`. The worker is a separate process with `SKIP LOCKED`, retries, and a dead-letter status. FastAPI `BackgroundTasks` are request-scoped and were rejected on purpose.

**Why not a plain `alerts` table?** You could. The outbox still earns its keep as the *relay* to an analytical engine that the API process must not share in memory with the UI.

**Forecast.** 48h velocity is used so a 36h spike is not diluted ~7× by a weekly average. This is not a statistical forecast (no MAPE, no seasonality). It is a transparent hourly walk: `stock + inbound − velocity`.

**Money.** Phase-1 catalogs carry margin for a reason. Headline the cockpit with contribution margin. List-price × units is `at_risk_gross_usd` in the JSON for anyone who still wants it.

**Clean Architecture.** Domain math has no SQLAlchemy/DuckDB imports. Adapters own Postgres and DuckDB. Streamlit depends only on HTTP.

**What this is not.** CDC/Debezium, exactly-once broker delivery, multi-tenant auth, Alembic migrations, or a replenishment optimizer. Production would add those; the tests here lock the parts that are easy to fake: same-TX outbox rollback, oversell, and “the PO actually changes the next simulation.”

## Tests that matter

- Outbox rows commit with the order and **disappear on rollback**
- Concurrent `place_order` cannot oversell (`FOR UPDATE`)
- Spike SKUs surface in 48h at-risk (7-day velocity is lower)
- Worker apply is idempotent
- Emergency PO reduces 48h shortfall on the next run
