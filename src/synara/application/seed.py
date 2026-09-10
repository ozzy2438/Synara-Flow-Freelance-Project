from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from synara.application.world import replace_world
from synara.data.generator import generate_world
from synara.infrastructure.duckdb_warehouse import DuckWarehouse


def seed_world(
    session: Session,
    warehouse: DuckWarehouse,
    *,
    seed: int = 42,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    catalog, orders = generate_world(seed=seed, now=now)
    products = []
    for sku in catalog:
        row = {
            "sku": sku.sku,
            "name": sku.name,
            "unit_price": sku.unit_price,
            "margin_rate": sku.margin_rate,
            "lead_time_days": sku.lead_time_days,
            "category": sku.category,
            "is_high_runner": sku.is_high_runner,
            "on_hand": sku.on_hand,
            "on_order": 0,
            "safety_stock": sku.safety_stock,
            "reorder_point": sku.reorder_point,
            "substitute_sku": None,
            "substitute_capture": None,
        }
        products.append(row)
    # One fixture pair so substitution is visible, not a second product.
    for p in products:
        if p["sku"] == "SKU-001":
            p["substitute_sku"] = "SKU-010"
            p["substitute_capture"] = 0.3
            break

    order_rows = [
        {
            "order_id": o.order_id,
            "sku": o.sku,
            "quantity": o.quantity,
            "unit_price": o.unit_price,
            "created_at": o.created_at,
            "idempotency_key": f"seed:{o.order_id}",
        }
        for o in orders
    ]
    replace_world(
        session,
        warehouse,
        products=products,
        orders=order_rows,
        now=now,
        mode="synthetic",
        source_label=f"Demo synthetic catalog (seed={seed}, 50 SKUs)",
    )
    return {
        "sku_count": len(catalog),
        "order_count": len(orders),
        "spiked_skus": [s.sku for s in catalog if s.spiked],
        "seed": seed,
        "as_of": now.isoformat(),
    }
