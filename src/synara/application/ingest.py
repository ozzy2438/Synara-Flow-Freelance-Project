"""CSV is the small-company system of record. The template is the current world."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synara.application.world import replace_world
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import InventoryRow, OrderRow, ProductRow


class CatalogLine(BaseModel):
    sku: str
    name: str
    unit_price: float = Field(gt=0)
    margin_rate: float = Field(ge=0, le=1)
    lead_time_days: int = Field(ge=0, le=365)
    on_hand: int = Field(ge=0)
    on_order: int = 0
    safety_stock: int = Field(ge=0)
    reorder_point: int = Field(ge=0)
    category: str = "general"
    is_high_runner: bool = False
    substitute_sku: str | None = None
    substitute_capture: float | None = Field(default=None, ge=0, le=1)

    @field_validator("on_order", mode="before")
    @classmethod
    def empty_on_order(cls, v: object) -> object:
        if v == "" or v is None:
            return 0
        return v

    @field_validator("is_high_runner", mode="before")
    @classmethod
    def truthy(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y"}
        return v

    @field_validator("substitute_sku", mode="before")
    @classmethod
    def blank_to_none(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("category", mode="before")
    @classmethod
    def category_or_general(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "general"
        return v

    @field_validator("substitute_capture", mode="before")
    @classmethod
    def blank_capture(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v


class SalesLine(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    created_at: datetime
    unit_price: float | None = None


def _read(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def ingest_csvs(
    session: Session,
    warehouse: DuckWarehouse,
    *,
    catalog_csv: str,
    sales_csv: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        catalog = [CatalogLine.model_validate(r) for r in _read(catalog_csv)]
        sales = [SalesLine.model_validate(r) for r in _read(sales_csv)]
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    if not catalog:
        raise ValueError("catalog.csv is empty")
    prices = {p.sku: p.unit_price for p in catalog}
    unknown = {s.sku for s in sales if s.sku not in prices}
    if unknown:
        raise ValueError(f"sales.csv references unknown SKUs: {sorted(unknown)[:8]}")
    products = [p.model_dump() for p in catalog]
    orders = [
        {
            "sku": s.sku,
            "quantity": s.quantity,
            "unit_price": s.unit_price if s.unit_price is not None else prices[s.sku],
            "created_at": s.created_at if s.created_at.tzinfo else s.created_at.replace(tzinfo=timezone.utc),
        }
        for s in sales
    ]
    replace_world(
        session,
        warehouse,
        products=products,
        orders=orders,
        now=now,
        mode="csv",
        source_label=f"Uploaded catalog.csv ({len(catalog)} SKUs) + sales.csv ({len(sales)} rows)",
    )
    return {"sku_count": len(catalog), "order_count": len(sales), "as_of": now.isoformat()}


def dump_csvs(session: Session) -> dict[str, str]:
    products = session.execute(select(ProductRow)).scalars().all()
    inventory = {i.sku: i for i in session.execute(select(InventoryRow)).scalars().all()}
    cat_buf = io.StringIO()
    cat_fields = [
        "sku",
        "name",
        "unit_price",
        "margin_rate",
        "lead_time_days",
        "on_hand",
        "on_order",
        "safety_stock",
        "reorder_point",
        "category",
        "is_high_runner",
        "substitute_sku",
        "substitute_capture",
    ]
    w = csv.DictWriter(cat_buf, fieldnames=cat_fields)
    w.writeheader()
    for p in products:
        inv = inventory[p.sku]
        w.writerow(
            {
                "sku": p.sku,
                "name": p.name,
                "unit_price": p.unit_price,
                "margin_rate": p.margin_rate,
                "lead_time_days": p.lead_time_days,
                "on_hand": inv.on_hand,
                "on_order": inv.on_order,
                "safety_stock": inv.safety_stock,
                "reorder_point": inv.reorder_point,
                "category": p.category,
                "is_high_runner": p.is_high_runner,
                "substitute_sku": p.substitute_sku or "",
                "substitute_capture": p.substitute_capture if p.substitute_capture is not None else "",
            }
        )
    sales_buf = io.StringIO()
    sw = csv.DictWriter(sales_buf, fieldnames=["sku", "quantity", "created_at", "unit_price"])
    sw.writeheader()
    orders = session.execute(select(OrderRow).order_by(OrderRow.created_at)).scalars().all()
    for o in orders:
        sw.writerow(
            {
                "sku": o.sku,
                "quantity": o.quantity,
                "created_at": o.created_at.isoformat(),
                "unit_price": o.unit_price,
            }
        )
    return {"catalog_csv": cat_buf.getvalue(), "sales_csv": sales_buf.getvalue()}


def last_sale_at(session: Session) -> datetime | None:
    return session.execute(select(func.max(OrderRow.created_at))).scalar_one_or_none()
