from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError

from synara.application.ingest import dump_csvs, ingest_csvs
from synara.application.orders import place_order
from synara.application.replenishment import export_po, place_emergency_po, po_number
from synara.application.seed import seed_world
from synara.application.simulate import run_simulation
from synara.config import Settings, get_settings
from synara.domain import DomainError, InsufficientStockError, SkuNotFoundError
from synara.infrastructure.db import (
    create_schema,
    is_retryable_db_error,
    make_engine,
    make_session_factory,
    session_scope,
)
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import count_pending_outbox


class OrderIn(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    idempotency_key: str = Field(min_length=4, max_length=128)


class OrderOut(BaseModel):
    order_id: str
    sku: str
    quantity: int


class SimulateIn(BaseModel):
    demand_multiplier: float = Field(default=1.0, ge=0.5, le=3.0)
    lead_time_delta_days: int = Field(default=0, ge=-5, le=21)
    horizon_hours: int = Field(default=48, ge=12, le=168)
    lost_sale_rate: float = Field(default=1.0, ge=0.0, le=1.0)


class ReplenishIn(BaseModel):
    sku: str
    quantity: int | None = Field(default=None, gt=0)


class IngestIn(BaseModel):
    catalog_csv: str = Field(min_length=8)
    sales_csv: str = Field(min_length=8)


def _retry(fn, attempts: int = 4):
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            last = exc
            if not is_retryable_db_error(exc) or i == attempts - 1:
                raise
            time.sleep(0.05 * (2**i))
    assert last
    raise last


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    factory = make_session_factory(engine)
    warehouse = DuckWarehouse(settings.duckdb_file)

    app = FastAPI(
        title="Synara Stockout Decision Engine",
        description=(
            "OLTP on Postgres with a transactional outbox. A dedicated worker "
            "relays events into a file-backed DuckDB OLAP sink. Streamlit is an "
            "API client — it does not open DuckDB itself."
        ),
        version="1.0.0",
    )
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.warehouse = warehouse

    def require_key(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        if not x_api_key or x_api_key != settings.api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
            )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/seed")
    def seed(auth: None = Depends(require_key), seed: int | None = None) -> dict[str, Any]:
        with session_scope(factory) as session:
            return seed_world(session, warehouse, seed=seed or settings.seed)

    @app.post("/api/orders", response_model=OrderOut)
    def create_order(body: OrderIn, auth: None = Depends(require_key)) -> OrderOut:
        def attempt() -> OrderOut:
            with session_scope(factory) as session:
                order = place_order(
                    session,
                    sku=body.sku,
                    quantity=body.quantity,
                    idempotency_key=body.idempotency_key,
                )
                return OrderOut(
                    order_id=str(order.id), sku=order.sku, quantity=order.quantity
                )

        try:
            return _retry(attempt)
        except InsufficientStockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SkuNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/simulate")
    def simulate(body: SimulateIn, auth: None = Depends(require_key)) -> dict[str, Any]:
        with session_scope(factory) as session:
            return run_simulation(
                session,
                warehouse,
                horizon_hours=body.horizon_hours,
                demand_multiplier=body.demand_multiplier,
                lead_time_delta_days=body.lead_time_delta_days,
                lost_sale_rate=body.lost_sale_rate,
            )

    @app.get("/api/alerts")
    def alerts(
        demand_multiplier: float = 1.0,
        lead_time_delta_days: int = 0,
        lost_sale_rate: float = 1.0,
        auth: None = Depends(require_key),
    ) -> dict[str, Any]:
        with session_scope(factory) as session:
            result = run_simulation(
                session,
                warehouse,
                demand_multiplier=demand_multiplier,
                lead_time_delta_days=lead_time_delta_days,
                lost_sale_rate=lost_sale_rate,
            )
            result["pending_outbox"] = count_pending_outbox(session)
            return result

    @app.post("/api/replenish")
    def replenish(body: ReplenishIn, auth: None = Depends(require_key)) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            with session_scope(factory) as session:
                po = place_emergency_po(
                    session,
                    sku=body.sku,
                    quantity=body.quantity,
                    buyer_email=settings.buyer_email,
                )
                return {
                    "po_id": str(po.id),
                    "po_number": po_number(po),
                    "sku": po.sku,
                    "quantity": po.quantity,
                    "expected_arrival": po.expected_arrival.isoformat(),
                    "buyer_email": po.buyer_email,
                    "channel": "csv_email_not_erp",
                    "note": (
                        "Persisted in Postgres. Not sent to an ERP. "
                        "POST /api/replenish/{po_id}/export for CSV + email draft."
                    ),
                }

        try:
            return _retry(attempt)
        except SkuNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/export")
    def export_world(auth: None = Depends(require_key)) -> dict[str, str]:
        with session_scope(factory) as session:
            return dump_csvs(session)

    @app.post("/api/ingest")
    def ingest(body: IngestIn, auth: None = Depends(require_key)) -> dict[str, Any]:
        try:
            with session_scope(factory) as session:
                return ingest_csvs(
                    session,
                    warehouse,
                    catalog_csv=body.catalog_csv,
                    sales_csv=body.sales_csv,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/replenish/{po_id}/export")
    def replenish_export(po_id: str, auth: None = Depends(require_key)) -> dict[str, Any]:
        try:
            with session_scope(factory) as session:
                return export_po(session, po_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Unknown PO") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


app = create_app()
