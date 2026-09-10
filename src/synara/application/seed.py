from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from synara.data.generator import generate_world
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import (
    InventoryRow,
    OrderRow,
    ProductRow,
    wipe_oltp,
)


def seed_world(
    session: Session,
    warehouse: DuckWarehouse,
    *,
    seed: int = 42,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    catalog, orders = generate_world(seed=seed, now=now)
    wipe_oltp(session)
    warehouse.reset()

    products_payload = []
    inventory_payload = []
    for sku in catalog:
        session.add(
            ProductRow(
                sku=sku.sku,
                name=sku.name,
                unit_price=sku.unit_price,
                margin_rate=sku.margin_rate,
                lead_time_days=sku.lead_time_days,
                category=sku.category,
                is_high_runner=sku.is_high_runner,
            )
        )
        session.add(
            InventoryRow(
                sku=sku.sku,
                on_hand=sku.on_hand,
                on_order=0,
                safety_stock=sku.safety_stock,
                reorder_point=sku.reorder_point,
                updated_at=now,
            )
        )
        products_payload.append(
            {
                "sku": sku.sku,
                "name": sku.name,
                "unit_price": sku.unit_price,
                "margin_rate": sku.margin_rate,
                "lead_time_days": sku.lead_time_days,
                "category": sku.category,
                "is_high_runner": sku.is_high_runner,
            }
        )
        inventory_payload.append(
            {
                "sku": sku.sku,
                "on_hand": sku.on_hand,
                "on_order": 0,
                "safety_stock": sku.safety_stock,
                "reorder_point": sku.reorder_point,
                "updated_at": now,
            }
        )

    fact_orders = []
    for o in orders:
        session.add(
            OrderRow(
                id=uuid.UUID(o.order_id) if _is_uuid(o.order_id) else uuid.uuid4(),
                sku=o.sku,
                quantity=o.quantity,
                unit_price=o.unit_price,
                created_at=o.created_at,
                idempotency_key=f"seed:{o.order_id}",
            )
        )
        oid = o.order_id
        fact_orders.append(
            {
                "event_id": f"seed-order-{oid}",
                "order_id": oid,
                "sku": o.sku,
                "quantity": o.quantity,
                "unit_price": o.unit_price,
                "created_at": o.created_at,
            }
        )

    session.flush()
    warehouse.replace_dimensions(products_payload, inventory_payload)
    warehouse.replace_facts_from_lists(fact_orders, [])
    spiked = [s.sku for s in catalog if s.spiked]
    return {
        "sku_count": len(catalog),
        "order_count": len(orders),
        "spiked_skus": spiked,
        "seed": seed,
        "as_of": now.isoformat(),
    }


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False
